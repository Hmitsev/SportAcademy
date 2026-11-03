# Academy.Manager by HMITSEVAPPS

Първа инсталация: Dyulgerov Academy.

## Качи в GitHub
- app.py
- requirements.txt
- render.yaml
- лого.jpg
- беграуд.jpg
- очаква се.jpg
- кодекс.jpg

## Render Environment
- DATABASE_URL = Neon PostgreSQL connection string
- ADMIN_EMAIL = администраторският имейл
- ADMIN_PASSWORD = временна силна парола, смени я преди първи вход
- ACADEMY_SLUG = dyulgerov-academy

## Включено в v0.1
- multi-academy база с academy_id
- 15 начални треньори
- набори 2012-2028
- родителска регистрация с 1-5 деца
- последващо добавяне на дете
- admin одобрение и match към група
- преместване на дете и смяна на треньор
- тренировки, мачове и групови съобщения
- месечни такси, начално 50 EUR, индивидуални корекции и форсмажор
- публични новини със снимка или видео линк
- бутон за картово плащане в изчакване

## Важно
Плащането с карта нарочно не е свързано към реален доставчик. След избор на myPOS, Stripe или друг доставчик се добавят защитен checkout, webhook и потвърждение на транзакцията.
