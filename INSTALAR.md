# Solicitar pelo Jellyfin · instalação

## 1. Indexador

```bash
cd /mnt/sdb1/strm-indexer
mkdir -p backup-$(date +%F) && cp api.py metadata_auto.py backup-$(date +%F)/
unzip -o strm-indexer-v09.zip
cp backend/api.py backend/jellyfin_bridge.py backend/metadata_auto.py .
```

Confira no `.env` (já existiam):

```
INDEXER_JELLYFIN_URL=http://host.docker.internal:8096
INDEXER_JELLYFIN_API_KEY=<chave de API do Jellyfin>
```

Opcionais:

```
INDEXER_JF_DAILY_LIMIT=20      # pedidos por usuário a cada 24 h (admins não têm limite)
INDEXER_JF_REFRESH_DELAY=60    # intervalo mínimo entre atualizações da biblioteca
INDEXER_JF_ORIGINS=            # só se o Jellyfin chamar o indexador em OUTRO domínio
```

Se você usar os opcionais, eles também precisam entrar no `environment:` do compose, como nos outros `docker-compose.v0xx.yml`.

```bash
docker compose restart
docker compose logs --tail=20 strm-indexer | grep JF_REQUEST   # deve mostrar "trabalhador iniciado"
```

## 2. Publicar o indexador como /strm-api no domínio do Jellyfin

O navegador chama `https://SEU-JELLYFIN/strm-api/...`. Assim não há problema de CORS nem de página HTTPS chamando um endereço HTTP.
Só o caminho `/jf/` do indexador é exposto; o painel administrativo continua fechado.

**Nginx**, dentro do `server { }` do Jellyfin:

```nginx
location /strm-api/ {
    proxy_pass http://127.0.0.1:8794/jf/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

**Nginx Proxy Manager**: no host do Jellyfin, aba *Custom locations* → Location `/strm-api/`, Forward `http://IP-DO-SERVIDOR:8794/jf/`.
Se o NPM roda em Docker, use o IP da máquina, não 127.0.0.1. Também confira se a porta 8794 do indexador aceita conexões vindas da rede do Docker, e não só de 127.0.0.1.

**Caddy**:

```
handle_path /strm-api/* {
    rewrite * /jf{uri}
    reverse_proxy 127.0.0.1:8794
}
```

Teste: abra `https://SEU-JELLYFIN/strm-api/health`. A resposta deve ser `{"ok": true, "jellyfin": true}`.

## 3. Plugin no Jellyfin 12

1. Painel → Plugins → Repositórios → adicionar
   `https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/12/manifest.json`
2. Catálogo → instale **JavaScript Injector** e reinicie o Jellyfin.
3. Painel → Plugins → JavaScript Injector → novo script → cole todo o `jellyfin/strm-solicitar.js` → marque ativo → salvar.
4. Recarregue o Jellyfin no navegador (Ctrl+Shift+R).

Se publicou o indexador em outro caminho ou domínio, ajuste `const API = '/strm-api';` no topo do script.

## Como funciona

- Na busca do Jellyfin aparece a faixa **"Disponível para solicitar"** com o que está nas listas e ainda não tem STRM.
- **Solicitar**: o indexador testa as fontes, uma a uma. A primeira que responder vira STRM (aprovação automática). Se nenhuma responder, o pedido fica como "Sem fonte".
- Séries: escolha a temporada (ou Todas). Os episódios sem fonte entram na verificação diária automática que já existia.
- Depois de adicionar, o indexador pede ao Jellyfin para atualizar a biblioteca (no máximo 1 vez por minuto).
- Painel do indexador → **Solicitações**: quem pediu, status, mensagem e o botão "Tentar de novo".
- Segurança: o script usa o token de login do próprio usuário, e o indexador confere esse token no Jellyfin (`/Users/Me`). Nenhuma chave fica no navegador e nenhuma URL de mídia é exposta.

## Limitações

- Funciona no Jellyfin web e nos apps que usam a interface web (celular, Jellyfin Media Player). Apps nativos de TV (Android TV, Roku, Tizen) não executam scripts injetados.
- O script procura a página de busca pela URL e pelo campo de pesquisa. Se uma atualização do Jellyfin mudar a estrutura da página e a faixa não aparecer, me mande um print e o HTML da página de busca.
- Títulos adicionados ao Jellyfin por fora do indexador ainda aparecem como "disponível para solicitar".
