import json
import time
from curl_cffi import requests
from pathlib import Path
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import random
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()


class CianParser:
    def __init__(self, headless=True):
        self.data_file = Path(__file__).parent.parent.parent  /'data'/'raw'/'cian_offers.jsonl'

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=headless)
        self.context = self.browser.new_context(
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        self.page = self.context.new_page()
        self.session = requests.Session(impersonate='chrome120')

    def generate_search_tasks(self):
        tasks = []
        base_query = {
            "_type": "flatsale",
            "engine_version": {"type": "term", "value": 2},
            'currency': {'type': 'term', 'value': 2}
        }

        districts = [
        {"name": "ЦАО", "id": 4},
        # {"name": "САО", "id": 5},
        # {"name": "СВАО", "id": 6},
        # {"name": "ВАО", "id": 7},
        # {"name": "ЮВАО", "id": 8},
        # {"name": "ЮАО", "id": 9},
        # {"name": "ЮЗАО", "id": 10},
        # {"name": "ЗАО", "id": 11},
        # {"name": "СЗАО", "id": 1},
        # {"name": "ЗелАО", "id": 151},
        # {"name": "НАО", "id": 325},
        # {"name": "ТАО", "id": 326}
        ]

        room_groups = [
            # {"name": "studio", "values": [9]},
            {"name": "1", "values": [1]},
            # {"name": "2", "values": [2]},
            # {"name": "3", "values": [3]},
            # {"name": "4", "values": [4]},
            # {"name": "5", "values": [5]}
        ]

        price_ranges = [
            (5000000, 10000000),
            (10000000, 20000000),
            (20000000, 30000000),
            (30000000, 50000000),
            (50000000, 100000000),
            (100000000, 200000000)
        ]

        for district in districts:
            for room_group in room_groups:
                for price_from, price_to in price_ranges:
                    query = base_query.copy()
                    query["price"] = {
                        "type": "range",
                        "value": {"gte": price_from, "lte": price_to}
                    }
                    query["geo"] = {
                        "type": "geo",
                        "value": [{"type": "district", "id": district["id"]}]
                    }
                    query["room"] = {"type": "terms", "value": room_group["values"]}
                    tasks.append(query)
        return tasks



    def _build_query_string(self, task, district_id, page):
        parts = [
            "currency=2",
            "deal_type=sale",
            f"district%5B0%5D={district_id}",
            "engine_version=2",

        ]
        if page > 1:
            parts.append(f'p={page}')
        if 'price' in task:
            price_val = task['price']['value']
            if 'lte' in price_val:
                parts.append(f"maxprice={price_val['lte']}")
            if 'gte' in price_val:
                parts.append(f"minprice={price_val['gte']}")
        parts.append("offer_type=flat")
        if 'room' in task:
            for room_val in task['room']['value']:
                parts.append(f"room{room_val}=1")

        return "&".join(parts)



    def _parse_ldjson(self, soup):
        ld_json_tag = soup.find('script', type='application/ld+json')
        result = {}
        if not ld_json_tag:
            return {}
        try:
            data = json.loads(ld_json_tag.string)
            if data.get('@type') == 'Product':
                result['id'] = data.get('sku')
                result['price'] = data['offers']['price']
                result['description'] = data['description']
                result['photos'] = data['image']
                name = data.get('name', '')
                rooms_match = re.search(r'(\d+)-комн', name)
                if rooms_match:
                    result['rooms'] = int(rooms_match.group(1))
                area_match = re.search(r'([\d,]+)\s*м²', name)
                if area_match:
                    area_str = area_match.group(1).replace(',', '.')
                    result['area_total'] = float(area_str)
                return result
        except Exception as e:
            print(f"Ошибка парсинга ld+json: {e}")
            return {}



    def _parse_factoids(self, soup):
        result = {}
        items = soup.find_all('div', attrs={'data-name': 'ObjectFactoidsItem'})
        for item in items:
            title_span = item.find('span', class_=re.compile('gray60'))
            value_span = item.find('span', class_=re.compile('bold'))
            if not (title_span and value_span):
                continue

            key = title_span.get_text(strip=True)
            value = value_span.get_text(strip=True)

            if 'Этаж' in key:
                match = re.search(r'(\d+)\s*из\s*(\d+)', value)
                if match:
                    result['floor'] = int(match.group(1))
                    result['floors_total'] = int(match.group(2))
                else:
                    result['floor'] = None
                    result['floors_total'] = None
            elif 'Жилая площадь' in key:
                result['area_living'] = float(value.split()[0].replace(',', '.'))
            elif 'Площадь кухни' in key:
                result['area_kitchen'] = float(value.split()[0].replace(',', '.'))
            elif 'Год постройки' in key:
                result['construction_year'] = int(value)

        return result

    def _parse_address(self, soup):
        addr_tag = soup.find('address')
        if addr_tag:
            return ' '.join(addr_tag.stripped_strings).replace('На карте', '').strip()
        return ''

    def _geocode_address(self, address: str) -> list | None:
        if not address:
            return None
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": address,
            "format": "json",
            "limit": 1
        }
        headers = {"User-Agent": "CianParser/1.0 (R89061187131@gmail.com)"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data:
                lat = float(data[0]['lat'])
                lon = float(data[0]['lon'])
                return [lat, lon]
        except Exception as e:
            print(f"Ошибка Nominatim: {e}")
        finally:
            time.sleep(1)
        return None



    def _parse_underground(self, soup):
        metros = []
        ul = soup.find('ul', attrs={'data-name': 'UndergroundList'})
        if not ul:
            return metros
        for li in ul.find_all('li', recursive=False):
            a = li.find('a')
            if not a:
                continue
            name = a.get_text(strip=True)
            time_span = li.find('span', class_=re.compile('underground_time'))
            time_text = None
            if time_span:
                time_text = ' '.join(time_span.stripped_strings)
            minutes = int(re.search(r'\d+', time_text).group()) if time_text else None
            metros.append({'name': name, 'time': minutes})
        return metros



    def _parse_features(self, soup):
        features = {}
        results = {}
        groups = soup.find_all('div', attrs={'data-testid': 'OfferSummaryInfoGroup'})
        for group in groups:
            items = group.find_all('div', attrs={'data-name': 'OfferSummaryInfoItem'})
            for item in items:
                p_tags = item.find_all('p')
                if len(p_tags) >= 2:
                    key = p_tags[0].get_text(strip=True)
                    value = p_tags[1].get_text(strip=True)
                    features[key] = value
        if 'Тип жилья' in features:
            results['flat_type'] = features.get('Тип жилья', '')
        if 'Высота потолков' in features:
            results['ceiling_height'] = float(features.get('Высота потолков').split()[0].replace(',', '.'))
        if 'Ремонт' in features:
            results['renovation_type'] = features.get('Ремонт')
        if 'Парковка' in features:
            results['parking'] = features.get('Парковка')
        if 'Аварийность' in features:
            results['accident_rate'] = features.get('Аварийность')
        if 'Количество лифтов' in features:
            results['elevators_count'] = features.get('Количество лифтов')
        if 'Отопление' in features:
            results['heating'] = features.get('Отопление')
        if 'Подъезды' in features:
            results['entrances'] = features.get('Подъезды')
        if 'Санузел' in features:
            results['bathroom'] = features.get('Санузел')
        if 'Тип дома' in features:
            results['house_type'] = features.get('Тип дома')

        return results



    def _parse_offer_page(self, html):
        soup = BeautifulSoup(html, 'lxml')
        result = {}

        result.update(self._parse_ldjson(soup))
        result.update(self._parse_factoids(soup))
        address = self._parse_address(soup)
        if address:
            result['address'] = address
            # lst = address.split(',')
            # address_for_geocode = ' '.join(lst[-2:] + [lst[0]])
            # result['coordinates'] = self._geocode_address(address_for_geocode)
        metros = self._parse_underground(soup)
        if metros:
            result['metros'] = metros
        if 'price' in result and 'area_total' in result and result['area_total'] > 0:
            result['price_per_m2'] = round(result['price'] / result['area_total'])
        result.update(self._parse_features(soup))

        return result



    def get_ids_from_page(self, url):
        try:
            self.page.goto(url, wait_until='domcontentloaded', timeout=15000)
            if self.page.query_selector('text=Ничего не найдено'):
                return []
            self.page.wait_for_selector('div[data-testid="offer-card"]', timeout=5000)

            last_count = 0
            while True:
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.6)

                cards = self.page.query_selector_all('div[data-testid="offer-card"]')
                current_count = len(cards)

                if current_count == last_count:
                    show_more = self.page.query_selector('a:has-text("Показать ещё"), button:has-text("Показать ещё")')
                    if not show_more:
                        show_more = self.page.query_selector(
                            '#frontend-serp > div > div > div:nth-child(6) > div.x31de4314--fb02f2--moreSuggestionsButtonContainer > a'
                        )
                    if show_more:
                        show_more.click()
                        try:
                            self.page.wait_for_function(
                                f'document.querySelectorAll("div[data-testid=\'offer-card\']").length > {current_count}',
                                timeout=1500
                            )
                        except:
                            pass
                        cards = self.page.query_selector_all('div[data-testid="offer-card"]')
                        if len(cards) > current_count:
                            last_count = len(cards)
                            continue
                        else:
                            break
                    else:
                        break
                else:
                    last_count = current_count

            ids = set()
            for card in cards:
                link = card.query_selector('a[href*="/sale/flat/"]')
                if link:
                    href = link.get_attribute('href')
                    match = re.search(r'/sale/flat/(\d+)', href)
                    if match:
                        ids.add(match.group(1))
            return list(ids)
        except Exception as e:
            print(f"Ошибка при загрузке {url}: {e}")
            return []



    def close(self):
        self.browser.close()
        self.playwright.stop()



    def collect_all_ids(self):
        all_ids = set()
        tasks = self.generate_search_tasks()
        for task_idx, task in enumerate(tasks):
            print(f"Задача {task_idx+1}/{len(tasks)}: {task}")
            district_id = task['geo']['value'][0]['id']
            page = 1
            while True:
                url = f"https://www.cian.ru/cat.php?{self._build_query_string(task, district_id, page)}"
                print(f"  Загружаем страницу {page}: {url}")
                start = time.time()
                ids = self.get_ids_from_page(url)
                if not ids:
                    print("  На странице нет объявлений, завершаем пагинацию.")
                    break
                before = len(all_ids)
                all_ids.update(ids)
                added = len(all_ids) - before
                print(f"  Страница {page}: добавилось {added} новых ID, всего {len(all_ids)}. Работало {time.time() - start:.0f} секунд")

                next_page_link = self.page.query_selector(f'a[href*="p={page + 1}"]')
                if not next_page_link:
                    print("  Следующая страница не найдена, завершаем.")
                    break

                page += 1
        print(f"Всего собрано уникальных ID: {len(all_ids)}")
        return list(all_ids)



    def get_offer_page(self, offer_id):
        time.sleep(random.uniform(2, 3))
        url = f"https://www.cian.ru/sale/flat/{offer_id}/"
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'Referer': 'https://www.cian.ru/',
        }
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        ]
        headers['User-Agent'] = random.choice(user_agents)
        try:
            response = self.session.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            return response.text
        except Exception as e:
            print(f'Ошибка загрузки ID {offer_id}: {e}')
            return None



    def get_offer_details(self, offer_id):
        html = self.get_offer_page(offer_id)
        if html:
            return self._parse_offer_page(html)
        return {}



    def collect_offers(self, max_workers=2):
        start_1 = time.time()
        ids = self.collect_all_ids()
        print(f'Сборка id работала {(time.time() - start_1)/60:.2f} минут')
        all_offers = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {executor.submit(self.get_offer_details, oid): oid for oid in ids}
            for future in tqdm(as_completed(future_to_id), total=len(ids), desc="Загрузка объявлений"):
                oid = future_to_id[future]
                try:
                    details = future.result()
                    if details:
                        all_offers.append(details)
                except Exception as e:
                    print(f"Ошибка при обработке ID {oid}: {e}")

        print(f"Успешно собрано объявлений: {len(all_offers)}.")
        print(f"Полная сборка работала {(time.time() - start_1) / 60:.2f} минут.")
        return all_offers



    def save_offers_in_jsonl(self, datas):
        self.data_file.parent.mkdir(parents=True, exist_ok=True)

        old_ids = set()
        if self.data_file.exists():
            with open(self.data_file, 'r', encoding='utf-8') as file:
                for line in file:
                    line = line.strip()
                    if line:
                        old_ids.add(json.loads(line)['id'])

        new_offers = []
        for offer in datas:
            if offer['id'] not in old_ids:
                new_offers.append(offer)

        if new_offers:
            with open(self.data_file, 'a', encoding='utf-8') as file:
                for item in new_offers:
                    file.write(json.dumps(item, ensure_ascii=False) + '\n')
            print(f"✅ Добавлено {len(new_offers)} новых объявлений")
        else:
            print("Новых объявлений нет")





if __name__ == '__main__':
    parser = CianParser()
    try:
        offers = parser.collect_offers()
        parser.save_offers_in_jsonl(offers)
    finally:
        parser.close()