from pathlib import Path
from database import init_db,ingest_claims,ingest_compensation
init_db()
if Path('baggage_claims.csv').exists():print('claims:',ingest_claims('baggage_claims.csv'))
for name in ('compensation_records.csv','passenger_discrepancies.csv'):
 if Path(name).exists():print('compensation:',ingest_compensation(name));break
print('Ready.')
