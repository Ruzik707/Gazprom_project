import json
import time
from curl_cffi import requests
from pathlib import Path
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

class CianParser:
    def __init__(self):
        self.session = requests.Session(impersonate='chrome')
        headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 YaBrowser/25.12.0.0 Safari/537.36',
            'Accept': '*/*',
            'Content-Type': 'application/json',
            'Origin': 'https://www.cian.ru',
            'Referer': 'https://www.cian.ru/',
            'Sec-Ch-Ua': '"Chromium";v="142", "YaBrowser";v="25.12", "Not_A_Brand";v="99", "Yowser";v="2.5"',
            'Sec-Ch-Ua-Mobile': '?',
            'Sec-Ch-Ua-Platform': '"Linux"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site'
            }
        self.session.headers.update(headers)
        self.data_file = Path(__file__).parent.parent.parent  /'data'/'raw'/'cian_offers.jsonl'

    def get_offer_ids(self, page):
        url = 'https://api.cian.ru/search-offers/v1/get-infinite-search-result-desktop/'
        payload = {"jsonQuery": {
                        "_type":"flatsale",
                        "engine_version": {"type": "term","value": 2},
                        "region": {"type": "terms", "value": [1]}
                  },
                  "queryString": "deal_type=sale&engine_version=2&offer_type=flat&region=1",
                  "pageNumber": page}

        try:
            response = self.session.post(url, json=payload, timeout=15)
            response.raise_for_status()
            return [item['itemId'] for item in response.json()['infiniteSearchResult']]
        except Exception as e:
            print(f'Ошибка при получении ID на странице {page}: {e}')
            return []

    def get_offer_page(self, offer_id):
        url = f"https://www.cian.ru/sale/flat/{offer_id}/"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            return response.text
        except Exception as e:
            print(f'Ошибка загрузки страницы с ID {offer_id}: {e}')
            return []

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

    def _geocode_address(self, address):
        if not address:
            return None
        url = "https://geocode-maps.yandex.ru/1.x/"
        params = {
            "apikey": "a8e99da4-0a44-4277-a614-9c989cd1ca9b",
            "geocode": address,
            "format": "json",
            "results": 1
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            feature_member = data['response']['GeoObjectCollection']['featureMember']
            if not feature_member:
                return None
            pos = feature_member[0]['GeoObject']['Point']['pos']
            lon, lat = map(float, pos.split())
            return [lat, lon]
        except Exception as e:
            print(f"Ошибка геокодирования адреса '{address}': {e}")
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
            result['coordinates'] = self._geocode_address(address)
        metros = self._parse_underground(soup)
        if metros:
            result['metros'] = metros
        if 'price' in result and 'area_total' in result:
            result['price_per_m2'] = round(result['price'] / result['area_total'])
        result.update(self._parse_features(soup))

        return result

    def get_offer_details(self, offer_id):
        html = self.get_offer_page(offer_id)
        if html:
            return self._parse_offer_page(html)
        return {}

    def collect_offers(self, max_pages=5, max_workers=3):
        all_ids = []
        for page in range(1, max_pages + 1):
            ids = self.get_offer_ids(page)
            if not ids:
                print(f"На странице {page} нет ID, завершаем.")
                break
            all_ids.extend(ids)
            time.sleep(2)

        all_offers = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {executor.submit(self.get_offer_details, oid): oid for oid in all_ids}
            for future in tqdm(as_completed(future_to_id), total=len(all_ids), desc="Загрузка объявлений"):
                oid = future_to_id[future]
                try:
                    details = future.result()
                    if details:
                        all_offers.append(details)
                except Exception as e:
                    print(f"Ошибка при обработке ID {oid}: {e}")

        print(f"Успешно собрано объявлений: {len(all_offers)}")
        return all_offers

    def save_offers_in_jsonl(self, datas):
        if self.data_file.exists():
            with open(self.data_file, 'r', encoding='utf-8') as file:
                old_ids = {json.loads(line)['id'] for line in file}
        else:
            old_ids = {}

        new_offers = []
        for offer in datas:
            if offer['id'] not in old_ids:
                new_offers.append(offer)

        if new_offers:
            with open('../../data/raw/cian_offers.jsonl', 'a', encoding='utf-8') as file:
                for item in new_offers:
                    file.write(json.dumps(item, ensure_ascii=False) + '\n')

    def download_photos(self, urls):
        pass





if __name__ == '__main__':
    parser = CianParser()

    offers = parser.collect_offers(1)
    parser.save_offers_in_jsonl(offers)