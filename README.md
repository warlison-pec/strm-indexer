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

## Séries monitoradas (episódios novos automáticos)

Como no Sonarr: ao abrir uma série no catálogo, ligue **"Monitorar episódios novos"** (a chave fica no quadro
"Importar em lote"; importar uma série já liga o monitoramento sozinho). A partir daí:

- as listas cadastradas por link são baixadas de novo a cada `INDEXER_WATCH_REFRESH_HOURS` horas (padrão 6; `0` desliga) e o catálogo é reindexado;
- sempre que o catálogo muda (por essa rotina ou por uma reindexação manual), os episódios que ainda não passaram
  pelo indexador são validados como na importação em lote e viram STRM; o Jellyfin é avisado para atualizar a biblioteca;
- cada episódio novo gera uma notificação (e o webhook, se configurado); os sem fonte válida entram na nova tentativa diária.

Se a série foi importada a partir de uma temporada, o monitoramento vale dessa temporada em diante.
A lista de séries monitoradas, com o botão **Verificar agora**, fica na aba **Gerenciados**.

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

### Contas Xtream: HLS no lugar de .ts

Em contas Xtream (links `http://servidor/usuario/senha/123` ou `/live/usuario/senha/123.ts`), o `.ts` ao vivo
costuma ser cortado pelo servidor a cada poucos segundos, e o player precisa reabrir. Ao testar o canal, o indexador
confere se a mesma conta entrega o canal em HLS (`/live/usuario/senha/123.m3u8`). Se entregar, o HLS passa a ser a
primeira opção (na lista direta, na proxy e na de TS contínuo para o Jellyfin) e o `.ts` vira reserva.

### Teste que confirma a falha

Uma fonte só é dada como falha se falhar duas vezes seguidas. Se a falha for do tipo passageiro (HTTP 429, 5xx,
tempo esgotado), um canal que já estava OK continua OK, com o aviso "Oscilou no teste".

### Lista para o Jellyfin (TS contínuo)

O ffmpeg do Jellyfin lida mal com canais HLS (`.m3u8`) ao vivo: quando a lista da fonte atrasa, ele
entende que o canal acabou e, com "Repetir automaticamente transmissões ao vivo" ligado, volta e repete
o trecho. Por isso o painel mostra um segundo link, **Lista para o Jellyfin** (`channels.m3u?t=...&fmt=ts`):
nele todos os canais passam pelo indexador como um fluxo **MPEG-TS contínuo**, como o Dispatcharr entrega.

- O indexador acompanha a lista HLS da fonte, começa perto do ao vivo e emenda os pedaços novos sem repetir.
- Pedaço perdido é pulado; se a fonte parar por 45 s, troca para a reserva sem fechar a conexão.
- Fontes com pedaços fMP4 ou criptografados não podem ser emendadas e retornam erro nesse link (use a lista normal).
- No sintonizador M3U do Jellyfin, deixe **"Repetir automaticamente transmissões ao vivo" desmarcado**.

### Cabeçalhos e fontes reserva

Muitos canais só respondem com um *User-Agent* ou *Referer* específico. O indexador entende os três formatos usados nas listas:
- `#EXTVLCOPT:http-user-agent=…` / `#EXTVLCOPT:http-referrer=…` (VLC, iptv-org);
- `#EXTHTTP:{"User-Agent": "…"}`;
- `http://canal|User-Agent=…&Referer=…` (Kodi).

Os testes usam esses cabeçalhos, então um canal no ar não aparece mais como falha só por falta deles.

**Buscar reservas** procura o mesmo canal em outras listas, pelo `tvg-id` (ignorando a variante `@HD`) ou pelo nome sem HD/FHD/(1080p). O resultado fica guardado como fonte reserva. No botão `⇄` do canal dá para ver as fontes, promover uma reserva a principal ou removê-la. O teste passa por todas as fontes, e o canal fica "OK" se alguma funcionar.

**Endereços dos canais na lista**:
- **Automático** (padrão): canais que exigem cabeçalhos ou têm reserva saem pelo indexador (`/strm-api/ch/<id>.m3u8|.ts`). Ele envia os cabeçalhos, repassa a lista HLS e os pedaços, e passa para a próxima fonte se uma não responder. Os outros vão direto para a fonte.
- **Sempre pelo indexador**: todos passam pelo servidor.
- **Sempre direto**: todos os links vão direto para a fonte, com os cabeçalhos em `#EXTVLCOPT`.

### Guia de programação (EPG)

Em **Fontes de guia (EPG)** você cadastra guias XMLTV (`.xml` ou `.xml.gz`).

- **Atualização:** o indexador baixa as fontes e atualiza a cada 12 h.
- **Associação:** cada canal da sua lista é ligado ao guia automaticamente, primeiro pelo `tvg-id` e depois pelo nome (ignorando HD/FHD/4K). Clicando em "Guia" no canal, dá para escolher à mão ou marcar "sem guia".
- **Guia publicado:** um único guia com **só os seus canais**, de 6 h atrás até 3 dias à frente (`/strm-api/channels.xml?t=…`). A lista M3U já aponta para ele no cabeçalho (`x-tvg-url`) e usa os mesmos `tvg-id`.
- **Arquivos grandes:** são lidos em fluxo; um guia de 70 MB é processado em segundos com pouca memória.

No Jellyfin: **Painel → TV ao vivo**, adicione a lista como sintonizador M3U e o guia como provedor XMLTV.

Por segurança, fontes em endereços internos (`192.168…`, `10…`, `localhost`) são bloqueadas. Para usar um gerador de EPG da sua rede, por exemplo um container em `http://192.168.1.16:3001/guide.xml`, libere só ele no `.env` e reinicie:

```
INDEXER_TRUSTED_HOSTS=192.168.1.16:3001
```

## Troca automática de fonte (filmes e episódios gerenciados)

O monitor diário confere cada item da aba **Gerenciados**. Quando a fonte de um item sai do ar (ou some da lista
depois de uma reindexação), o indexador procura o **mesmo conteúdo** no catálogo, em qualquer lista: o mesmo filme
(nome e ano) ou o mesmo episódio (série, temporada e episódio). Ele testa as opções (primeiro as de outro servidor)
e troca para a primeira que funcionar, na hora, sem esperar as falhas seguidas.

- O STRM aponta para `/play/<chave>` do indexador, então o Jellyfin não percebe a troca: progresso e "visto" continuam.
- Cada troca aparece nas notificações ("Fonte trocada automaticamente: lista A → lista B") e no webhook, se configurado.
- Nunca troca para uma categoria bloqueada no filtro de conteúdo.
- Sem nenhuma alternativa funcionando, vale a regra de antes: após `INDEXER_FAILURE_LIMIT` falhas seguidas o item vai
  para a quarentena, e volta sozinho se alguma fonte (a antiga ou uma nova) voltar.
- `INDEXER_REPLACEMENT_TRIES` (padrão 12) limita quantas opções são testadas por item a cada verificação.

## Varredura automática de metadados

Antes, capa, sinopse e o código do TMDB só eram buscados para os títulos que apareciam na tela do painel.
Agora uma varredura em segundo plano mantém a fila de metadados abastecida mesmo sem ninguém acessar:

1. primeiro os itens da aba **Gerenciados** (os que já viraram STRM);
2. depois o catálogo inteiro, em ordem alfabética, continuando de onde parou (inclusive após reiniciar);
3. ao chegar ao fim, espera 24 h e recomeça, pegando os títulos novos das listas.

O ritmo é o do trabalhador que já existia (um item a cada `INDEXER_META_GAP_SECONDS`, padrão 12 s), com no máximo
40 itens esperando na fila, para não esbarrar nos limites do TMDB/TVDB. Categorias bloqueadas no filtro de conteúdo
ficam de fora.

- `INDEXER_META_SCAN=0` desliga a varredura (volta ao comportamento antigo).
- `INDEXER_META_SCAN_SECONDS` (padrão 300) define de quanto em quanto tempo a fila é reabastecida.

## Limpeza semanal de links mortos

Uma vez por semana, de madrugada, o indexador testa uma parte dos links das listas (filmes, episódios e canais
de TV) e exclui do catálogo e da aba Canais os que estão fora do ar.

- **Só com certeza:** um link só é excluído depois de falhar em duas rodadas seguidas; erro passageiro (429, 5xx,
  tempo esgotado) não conta. Os que falharam uma vez são conferidos primeiro na rodada seguinte.
- **Por partes:** cada rodada testa até `INDEXER_PRUNE_MAX` links (padrão 20000), no máximo 2 conexões por servidor
  e por até `INDEXER_PRUNE_HOURS` horas (padrão 4); a próxima continua de onde parou até cobrir tudo.
- **Os arquivos M3U não são alterados:** as listas por link são baixadas de novo e trariam as linhas de volta. O
  indexador guarda os links mortos (`data/link_health.sqlite`) e os ignora em toda reindexação. **Restaurar
  excluídos** (aba Listas) desfaz.
- **Não testa:** itens da aba Gerenciados (o monitor diário já cuida deles, inclusive trocando a fonte) e endereços
  de `INDEXER_TRUSTED_HOSTS` (ex.: Dispatcharr na rede local).
- Agenda: `INDEXER_PRUNE_DAY` (0 = segunda … 6 = domingo, padrão 6; `off` desliga) e `INDEXER_PRUNE_HOUR`
  (padrão 3). O horário segue o fuso do contêiner (defina `TZ=America/Sao_Paulo` no compose).

