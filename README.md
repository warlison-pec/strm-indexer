# STRM Indexer v0.1 — teste isolado

Catálogo local M3U/M3U8 com suporte a **vários arquivos** (sem limite fixo configurado).

## Instalação no BoxServer

1. Descompacte em `/mnt/sdb1/strm-indexer`.
2. Copie suas listas M3U/M3U8 autorizadas para `/mnt/sdb1/strm-indexer/playlists/`. Cada arquivo torna-se uma fonte independente. Nomes devem ser únicos.
3. Execute a importação **antes** de subir a API:

```bash
cd /mnt/sdb1/strm-indexer
docker run --rm -v "$PWD:/app" -w /app -e INDEXER_PLAYLIST_DIR=/app/playlists -e INDEXER_DB=/app/data/catalog.sqlite python:3.12-slim python indexer.py import
docker compose up -d
```

4. Teste:

```bash
curl -s http://127.0.0.1:8794/health
curl -s http://127.0.0.1:8794/stats
curl -sG http://127.0.0.1:8794/search --data-urlencode 'title=O Rebelde' --data-urlencode 'type=movie' --data-urlencode 'exact=1'
curl -sG http://127.0.0.1:8794/search --data-urlencode 'title=Rooster Fighter' --data-urlencode 'type=episode' --data-urlencode 'season=1' --data-urlencode 'episode=2'
```

## Adicionar novas listas

Copie novos arquivos `.m3u` ou `.m3u8` em `playlists/` e execute novamente o comando `docker run ... indexer.py import`. A importação monta um banco novo e substitui atomicamente o antigo **somente após sucesso**. A API utiliza conexões novas por consulta; não requer reinicialização. Remover uma lista e reimportar também remove seus registros do índice.

## Segurança e escopo

- **Nenhum** serviço STRM Manager, STRM Series ou Jellyfin é modificado nesta versão.
- Somente `.m3u` e `.m3u8` **locais** são lidos. Nenhuma URL remota é baixada pelo indexador.
- Busca retorna apenas metadados e hostname, não URLs completas.
- `/source?id=...` retorna a URL original **não validada**, apenas com `Authorization: Bearer <INDEXER_TOKEN>` (defina em `.env`); só para integrações internas futuras.
- API publicada apenas em `127.0.0.1:8794` do host. Para acesso entre containers, configurar explicitamente rede/hostname em etapa posterior.
- O catálogo não valida autorização de acesso, direitos de reprodução, conteúdo dos segmentos nem expiração dos links. Isso deve ser tratado por um validador separado, antes de qualquer integração de reprodução.
- Títulos de episódios são identificados via `S01E02`; outros formatos e anos ausentes precisam de revisão.
- `year` nos registros é extraído de nomes e pode faltar.
- O volume `./` é montado como leitura; apenas `./data` é gravável.
