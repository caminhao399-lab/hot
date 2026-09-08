# HOT — Telegram VIP Bot

Bot de assinatura para usuários adultos (18+), com confirmação de idade, pagamento em Telegram Stars, registro de assinaturas, criação de links privados e rotina de expiração.

## Stack

- Python 3.13
- aiogram 3.31
- FastAPI + Uvicorn
- SQLite para o modo inicial
- Render Web Service

## Variáveis obrigatórias no Render

- `BOT_TOKEN` — token do BotFather. **Nunca coloque esse token no GitHub.**
- `CHANNEL_ID` — ID ou @username do canal privado.
- `GROUP_ID` — ID ou @username do grupo privado (opcional).
- `ADMIN_ID` — seu ID numérico do Telegram para comandos administrativos.

## Variáveis opcionais

- `BOT_USERNAME` — padrão `porno_sexy_bot`.
- `PRICE_STARS` — preço em Telegram Stars; padrão `100`.
- `SUBSCRIPTION_DAYS` — duração; padrão `30`.
- `SUPPORT_USERNAME` — usuário de suporte sem `@`.
- `DB_PATH` — padrão `/tmp/hot_bot.sqlite3` no Blueprint inicial.

## Render

O Blueprint já define:

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`

Depois de conectar o repositório ao Render, preencha as variáveis marcadas como secretas e faça o deploy.

## Telegram

O bot precisa ser administrador no canal/grupo privado para criar links e remover membros expirados. Para pagamentos de conteúdo digital dentro do Telegram, o código usa Telegram Stars (`XTR`).

## Fluxo

1. `/start`
2. Confirmação 18+
3. Escolha de assinatura
4. Invoice em Telegram Stars
5. Confirmação do pagamento
6. Criação de convite individual para os chats configurados
7. Registro da validade
8. Rotina periódica de expiração/revogação

## Importante

O conteúdo vendido deve ser legal, destinado exclusivamente a adultos e compatível com as regras do Telegram e as leis aplicáveis. O bot não armazena conteúdo adulto no repositório.
