import pandas as pd
import json
from pathlib import Path
from schema import CianSchema


def validate_and_save_parquet(jsonl_path, parquet_path):
    offers = []
    with open(jsonl_path, 'r', encoding='utf-8') as file:
        for line in file:
            offers.append(json.loads(line))

    df = pd.DataFrame(offers)

    if 'description' in df.columns:
        df = df.rename(columns={'description': 'offer_description'})

    try:
        validated_df = CianSchema.validate(df)
        print('Валидация прошла успешно')
    except Exception as e:
        print(f'Ошибка валидации: {e}')
        return

    validated_df.to_parquet(parquet_path, index=False)
    print(f'Сохранен в типе parquet в {parquet_path}')



if __name__ == '__main__':
    jsonl_path = Path(__file__).parent.parent.parent / 'data' / 'raw' / 'cian_offers.jsonl'
    parquet_path = Path(__file__).parent.parent.parent / 'data' / 'processed' / 'cian_offers.parquet'

    validate_and_save_parquet(jsonl_path, parquet_path)