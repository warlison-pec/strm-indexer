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

## Filtro de conteúdo (categorias bloqueadas)

No painel, o botão **Filtro** (ícone de escudo) bloqueia categorias inteiras, por exemplo canais adultos:

- **Bloqueio automático** (ligado por padrão) de categorias com nomes como *Adultos*, *XXX*, *+18*, *Hot* e *Erótico*. Clicar numa categoria detectada libera uma exceção.
- **Bloqueio manual** de qualquer outra categoria.
- O conteúdo bloqueado **não aparece** na busca/solicitação do Jellyfin (script `strm-solicitar.js`), do app StreamBox e do StreamBox Web, **não pode ser solicitado** e não é usado na importação de episódios.
- No painel ele também fica oculto, a não ser que a opção *Mostrar o conteúdo bloqueado neste painel* esteja ligada.

A configuração fica em `data/content_filter.json`.

## Canais ao vivo (lista própria de IPTV)

A aba **Canais** funciona como o "Channels" do Dispatcharr:

- **À esquerda** ficam todos os canais ao vivo encontrados nas listas, com logo, grupo e lista de origem. Dá para filtrar por nome, lista e grupo, e adicionar um a um ou o grupo inteiro de uma vez. Filmes e episódios não entram; os grupos bloqueados pelo *Filtro de conteúdo* também não.
- **À direita** fica a sua seleção:
  - arrastar para reordenar, numerar os canais e editar nome ou grupo (clique no nome);
  - **testar** todos, só os novos ou um por um; o teste roda em segundo plano e mostra o progresso;
  - remover de uma vez os canais com falha.
- **Lista gerada**: um endereço M3U protegido por chave (`/strm-api/channels.m3u?t=…`), que pode ir para:
  - a TV ao vivo do Jellyfin (sintonizador M3U);
  - a "Minha lista" do StreamBox;
  - qualquer player IPTV.

  Também dá para informar o guia (EPG/XMLTV) e escolher incluir só canais testados e funcionando, que é o padrão. Se o link vazar, gere uma nova chave.

A seleção fica em `data/channels.sqlite`, com cópia dos dados de cada canal, e os logos ficam guardados em `data/channel_logos/`.
