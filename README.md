# 🚂 Factorio Discord Bot

Bot do Discord para monitorar e interagir com um servidor headless de Factorio 2.0 (Space Age). Roda na **mesma máquina** que o servidor Factorio.

## Funcionalidades

| Comando | Descrição |
|---------|-----------|
| `!status` | Mostra se o servidor está online, quantos jogadores, tempo de jogo |
| `!players` | Lista os jogadores conectados no momento |
| `!cmd <comando>` | Executa um comando no console do servidor via RCON |
| `!help` | Mostra esta lista de comandos |

**Ponte de chat bidirecional:**
- Mensagens no chat do Factorio → aparecem no Discord
- Mensagens no canal do Discord (sem `!`) → aparecem no jogo como `[Discord] Nome: msg`

**Notificações automáticas:**
- 🟢 **Servidor ligado!** quando o servidor inicia
- 🛑 **Servidor desligado!** quando o servidor para

## Como funciona

O bot usa **duas abordagens** para se comunicar com o Factorio:

### RCON (para executar comandos)
- Conecta na porta RCON do servidor (TCP 34198)
- Usa o protocolo Source RCON (com uma peculiaridade: o password não leva null terminator no pacote de auth)
- Útil para comandos como `!cmd help`, `!cmd /c game.speed=2`

### Screen + Lua logging (para ler estado do jogo)
- Envia comandos `/c` Lua diretamente pro stdin do processo do Factorio via `screen -X stuff`
- Usa `log()` pra escrever resultados no `factorio-current.log`
- Lê o log pra extrair dados como número de jogadores, nomes, ticks
- Usa tags únicas por consulta (timestamp ms) pra evitar ler resultados de consultas anteriores

### Monitoramento de log (para chat e notificações)
- Monitora o `server.log` em busca de entradas `[CHAT]` pra fazer a ponte Factorio → Discord
- Verifica o processo do Factorio a cada 10s pra detectar quando liga/desliga

## Estrutura

```
factorio-discord-bot/
├── bot.py              # Bot principal (discord.py)
├── .env                # Configurações (NÃO versionar)
├── .env.example        # Template de configuração
└── README.md           # Este arquivo
```

## Configuração

### 1. Criar um bot no Discord
1. Acesse https://discord.com/developers/applications
2. New Application → dê um nome
3. Vá em Bot → Reset Token e copie o token
4. Em OAuth2 → URL Generator → marque `bot` + permissões `Send Messages`, `Read Message History`
5. Abra a URL gerada e adicione o bot ao servidor

### 2. Configurar o `.env`

```env
DISCORD_TOKEN=seu_token_aqui
CHANNEL_ID=id_do_canal_discord
RCON_HOST=127.0.0.1
RCON_PORT=34198
RCON_PASSWORD=senha_do_rcon
SERVER_LOG=~/factorio/server.log
FACTORIO_LOG=~/factorio/factorio/factorio-current.log
```

### 3. Dependências

```bash
pip install discord.py python-dotenv
```

### 4. Rodar

```bash
python3 bot.py
```

Ou via screen (recomendado para manter rodando):
```bash
screen -dmS factorio-bot bash -c 'cd /caminho/do/bot && python3 bot.py'
```

## Requisitos do Servidor Factorio

- Factorio headless 2.0+ rodando com `--rcon-port` e `--rcon-password`
- O bot precisa rodar na **mesma máquina** que o servidor (acessa o processo via `screen` e lê os logs localmente)

## Gerenciamento

Para gerenciar o servidor + bot, use o script `factorio-control.sh` incluso no servidor (VPS).

```bash
~/factorio/factorio-control.sh start       # inicia o servidor
~/factorio/factorio-control.sh stop        # para o servidor
~/factorio/factorio-control.sh bot-start   # inicia o bot
~/factorio/factorio-control.sh bot-stop    # para o bot
~/factorio/factorio-control.sh status      # status do servidor
~/factorio/factorio-control.sh bot-status  # status do bot
```

## Peculiaridades conhecidas

- **RCON auth sem null terminator:** Diferente do protocolo Source RCON padrão, o Factorio espera o password sem o byte nulo (`\x00`) no fim do corpo do pacote de autenticação
- **Lua `/c` via RCON não funciona:** Os comandos `/c` retornam erro de sintaxe quando enviados via RCON. Por isso o bot usa `screen -X stuff` para enviar comandos Lua diretamente para o console do processo
- **UDP tracking:** Não é possível usar `ss` para detectar jogadores conectados porque o Factorio usa UDP não-conectado. O bot usa `log()` via Lua para consultar `game.connected_players`
