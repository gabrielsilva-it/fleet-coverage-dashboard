"""Gera os dois painéis do parque (InfluxDB "parque" gravado pelo parque_coletor.py):
  grafana_parque_tv.json       "Parque - TV": números e pendências, SEM usuário e SEM IP (pode ir para a TV)
  grafana_parque_detalhe.json  "Parque - Detalhe": tabela máquina a máquina com usuário, IP e filtros (só TI)
Mesmo estilo do painel "Kaspersky - TV". Na importação o Grafana pergunta a fonte (DS_PARQUE).
    python gen_painel_parque.py
"""
import json
import os
import sys

DS = {'type': 'influxdb', 'uid': '${DS_PARQUE}'}
AGORA = 'time > now() - 30m'   # última coleta (o coletor roda a cada 10 min)
VERDE, VERMELHO, AMARELO, LARANJA, AZUL, CINZA = '#2E9E6B', '#D64545', '#D9A441', '#E08A3C', '#3F7FBF', '#5C6773'


def alvo(q, ref='A', fmt='time_series', alias=None):
    t = {'refId': ref, 'datasource': DS, 'query': q, 'rawQuery': True, 'resultFormat': fmt}
    if alias:
        t['alias'] = alias
    return t


def passos(*pares):
    return {'mode': 'absolute', 'steps': [{'color': c, 'value': v} for v, c in pares]}


def painel(id_, tipo, titulo, x, y, w, h, **kw):
    p = {'id': id_, 'type': tipo, 'title': titulo, 'datasource': DS, 'transparent': True,
         'gridPos': {'x': x, 'y': y, 'w': w, 'h': h}}
    p.update(kw)
    return p


def stat_opts(titulo, valor, modo='background_solid', grafico='none', texto='value', campos=''):
    return {'reduceOptions': {'calcs': ['lastNotNull'], 'fields': campos, 'values': False},
            'colorMode': modo, 'graphMode': grafico, 'textMode': texto, 'justifyMode': 'center',
            'orientation': 'auto', 'wideLayout': True, 'text': {'titleSize': titulo, 'valueSize': valor}}


def pct(campo, titulo, x, w, descricao):
    return painel(10 + x, 'stat', titulo, x, 2, w, 5, description=descricao,
                  targets=[alvo(f'SELECT last("pct_{campo}") FROM "parque_geral" WHERE $timeFilter GROUP BY time(30m) fill(none)')],
                  options=stat_opts(14, 44, grafico='area'),
                  fieldConfig={'defaults': {'unit': 'percent', 'decimals': 0, 'min': 0, 'max': 100, 'noValue': 'SEM DADOS',
                                            'color': {'mode': 'thresholds'},
                                            'thresholds': passos((None, VERMELHO), (85, AMARELO), (97, VERDE))},
                               'overrides': []})


def cabecalho(titulo):
    return [
        painel(1, 'text', '', 0, 0, 14, 2, datasource=None, options={'mode': 'markdown', 'content': f'# {titulo}\n'}),
        painel(2, 'stat', '', 14, 0, 10, 2,
               targets=[alvo('SELECT last("ativas") FROM "parque_geral" WHERE time > now() - 2h')],
               options=stat_opts(12, 22, modo='value', campos='/^Time$/'),
               fieldConfig={'defaults': {'unit': 'time:[Atualizado em] DD/MM [às] HH:mm', 'noValue': 'SEM DADOS',
                                         'color': {'mode': 'fixed', 'fixedColor': CINZA}}, 'overrides': []}),
    ]


def numeros():
    return [
        painel(3, 'stat', 'ESTAÇÕES EM USO', 0, 2, 4, 5,
               description='Estações do domínio com logon nos últimos 30 dias (base de tudo). Embaixo: total de estações no AD.',
               targets=[alvo(f'SELECT last("ativas") AS "EM USO", last("ad_estacoes") AS "NO AD" FROM "parque_geral" WHERE {AGORA}')],
               options=stat_opts(13, 34, texto='value_and_name'),
               fieldConfig={'defaults': {'unit': 'none', 'decimals': 0, 'noValue': 'SEM DADOS',
                                         'color': {'mode': 'fixed', 'fixedColor': AZUL}},
                            # o Influx devolve "parque_geral.EM USO": casar pelo fim do nome e renomear
                            'overrides': [{'matcher': {'id': 'byRegexp', 'options': '.*EM USO$'},
                                           'properties': [{'id': 'displayName', 'value': 'EM USO'}]},
                                          {'matcher': {'id': 'byRegexp', 'options': '.*NO AD$'},
                                           'properties': [{'id': 'displayName', 'value': 'NO AD'},
                                                          {'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': CINZA}}]}]}),
        pct('completas', 'COMPLETAS (AS 3)', 4, 5, 'Estações em uso com Kaspersky, Tactical e GLPI respondendo nos últimos 7 dias.'),
        pct('kaspersky', 'KASPERSKY', 9, 5, 'Estações em uso com o agente do KSC e o KES falando nos últimos 7 dias.'),
        pct('tactical', 'TACTICAL', 14, 5, 'Estações em uso com o agente do Tactical falando nos últimos 7 dias.'),
        pct('glpi', 'GLPI', 19, 5, 'Estações em uso com o GLPI Agent mandando inventário nos últimos 7 dias.'),
    ]


def unidades(y, h):
    return painel(20, 'bargauge', 'UNIDADES · % COMPLETAS', 0, y, 9, h,
                  targets=[alvo(f'SELECT last("pct_completas") FROM "parque_unidade" WHERE {AGORA} GROUP BY "unidade"',
                                alias='$tag_unidade')],
                  options={'orientation': 'horizontal', 'displayMode': 'basic', 'showUnfilled': True, 'valueMode': 'color',
                           'namePlacement': 'left', 'minVizHeight': 16, 'maxVizHeight': 40,
                           'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
                           'text': {'titleSize': 16, 'valueSize': 18}},
                  fieldConfig={'defaults': {'unit': 'percent', 'decimals': 0, 'min': 0, 'max': 100, 'noValue': '-',
                                            'color': {'mode': 'thresholds'},
                                            'thresholds': passos((None, VERMELHO), (85, AMARELO), (97, VERDE))},
                               'overrides': []})


def pendencias(y, h):
    cores = {'Sem Kaspersky': VERMELHO, 'Sem Tactical': LARANJA, 'Sem GLPI': AMARELO,
             'Fora do AD (nas ferramentas)': AZUL, 'Sem logon no AD > 90 dias': CINZA}
    mapa = {'type': 'value', 'options': {n: {'text': n.upper(), 'color': c, 'index': i} for i, (n, c) in enumerate(cores.items())}}
    return painel(21, 'table', 'PENDÊNCIAS · O QUE FALTA EM CADA MÁQUINA', 9, y, 15, h,
                  targets=[alvo('SELECT last("qtd") AS "QTD", last("maquinas") AS "MÁQUINAS", last("ordem") AS "ordem" '
                                f'FROM "parque_pendencia" WHERE {AGORA} GROUP BY "motivo"', fmt='table')],
                  transformations=[
                      {'id': 'filterByValue', 'options': {'type': 'include', 'match': 'any', 'filters': [
                          {'fieldName': 'QTD', 'config': {'id': 'greater', 'options': {'value': 0}}}]}},
                      {'id': 'sortBy', 'options': {'sort': [{'field': 'ordem', 'desc': False}]}},
                      {'id': 'organize', 'options': {'excludeByName': {'Time': True, 'ordem': True},
                                                     'renameByName': {'motivo': 'PENDÊNCIA'},
                                                     'indexByName': {'motivo': 0, 'QTD': 1, 'MÁQUINAS': 2}}}],
                  options={'showHeader': True, 'cellHeight': 'lg', 'footer': {'show': False}},
                  fieldConfig={'defaults': {'custom': {'align': 'left', 'cellOptions': {'type': 'auto'}, 'inspect': False},
                                            'noValue': 'TUDO CERTO', 'color': {'mode': 'fixed', 'fixedColor': CINZA}},
                               'overrides': [
                                   {'matcher': {'id': 'byName', 'options': 'PENDÊNCIA'},
                                    'properties': [{'id': 'mappings', 'value': [mapa]},
                                                   {'id': 'custom.cellOptions', 'value': {'type': 'color-background', 'mode': 'basic'}},
                                                   {'id': 'custom.width', 'value': 270}]},
                                   {'matcher': {'id': 'byName', 'options': 'QTD'},
                                    'properties': [{'id': 'custom.width', 'value': 70}, {'id': 'custom.align', 'value': 'center'},
                                                   {'id': 'decimals', 'value': 0}]}]})


def tabela_detalhe(y):
    campos = ('usuario', 'ip', 'situacao', 'faltando', 'ad_dias', 'tactical_dias', 'glpi_dias', 'ksc_dias',
              'ksc_ok', 'tactical_ok', 'glpi_ok', 'so', 'modelo', 'serial', 'ou', 'glpi_estado', 'glpi_cadastros')
    sel = ', '.join(f'last("{c}") AS "{c}"' for c in campos)
    nomes = {'maquina': 'Máquina', 'unidade': 'Unidade', 'tipo': 'Tipo', 'usuario': 'Usuário', 'ip': 'IP',
             'situacao': 'Situação', 'faltando': 'Falta', 'ad_dias': 'Logon AD (dias)', 'tactical_dias': 'Tactical (dias)',
             'glpi_dias': 'GLPI (dias)', 'ksc_dias': 'Kaspersky (dias)', 'ksc_ok': 'Kaspersky', 'tactical_ok': 'Tactical',
             'glpi_ok': 'GLPI', 'so': 'Sistema', 'modelo': 'Modelo', 'serial': 'Série', 'ou': 'OU',
             'glpi_estado': 'Estado no GLPI', 'glpi_cadastros': 'Cadastros GLPI'}
    ordem = ['maquina', 'unidade', 'usuario', 'ip', 'situacao', 'ksc_ok', 'tactical_ok', 'glpi_ok', 'ad_dias',
             'ksc_dias', 'tactical_dias', 'glpi_dias', 'faltando', 'tipo', 'so', 'modelo', 'serial', 'glpi_estado',
             'glpi_cadastros', 'ou']
    simnao = [{'type': 'value', 'options': {'true': {'text': '✔', 'color': VERDE, 'index': 0},
                                            'false': {'text': '✖', 'color': VERMELHO, 'index': 1}}}]
    sit = [{'type': 'value', 'options': {'Completa': {'color': VERDE, 'index': 0}, 'Fora do AD': {'color': AZUL, 'index': 1},
                                         'Inativa no AD': {'color': CINZA, 'index': 2}}},
           {'type': 'regex', 'options': {'pattern': '^Falta.*', 'result': {'color': LARANJA, 'index': 3}}}]
    return painel(30, 'table', 'MÁQUINAS · USE O FILTRO DE CADA COLUNA (FUNIL NO CABEÇALHO)', 0, y, 24, 22,
                  targets=[alvo(f'SELECT {sel} FROM "parque_maquina" WHERE {AGORA} AND "unidade" =~ /^$unidade$/ '
                                f'AND "tipo" =~ /^$tipo$/ GROUP BY "maquina", "unidade", "tipo"', fmt='table')],
                  transformations=[{'id': 'organize', 'options': {
                      'excludeByName': {'Time': True}, 'renameByName': nomes,
                      'indexByName': {c: i for i, c in enumerate(ordem)}}}],
                  options={'showHeader': True, 'cellHeight': 'sm', 'footer': {'show': True, 'reducer': ['count'],
                                                                              'countRows': True, 'fields': ''},
                           'sortBy': [{'displayName': 'Situação', 'desc': True}]},
                  fieldConfig={'defaults': {'custom': {'align': 'left', 'filterable': True, 'cellOptions': {'type': 'auto'},
                                                       'inspect': True}, 'noValue': '-'},
                               'overrides': [
                                   *[{'matcher': {'id': 'byName', 'options': n},
                                      'properties': [{'id': 'mappings', 'value': simnao}, {'id': 'custom.align', 'value': 'center'},
                                                     {'id': 'custom.width', 'value': 90},
                                                     {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}}]}
                                     for n in ('Kaspersky', 'Tactical', 'GLPI')],
                                   {'matcher': {'id': 'byName', 'options': 'Situação'},
                                    'properties': [{'id': 'mappings', 'value': sit},
                                                   {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}},
                                                   {'id': 'custom.width', 'value': 210}]},
                                   *[{'matcher': {'id': 'byName', 'options': n},
                                      'properties': [{'id': 'decimals', 'value': 0}, {'id': 'custom.width', 'value': 110},
                                                     {'id': 'custom.align', 'value': 'center'}]}
                                     for n in ('Logon AD (dias)', 'Kaspersky (dias)', 'Tactical (dias)', 'GLPI (dias)')],
                                   {'matcher': {'id': 'byName', 'options': 'Máquina'},
                                    'properties': [{'id': 'custom.width', 'value': 150}]}]})


def variavel(nome, rotulo, consulta):
    return {'name': nome, 'label': rotulo, 'type': 'query', 'datasource': DS, 'query': consulta, 'refresh': 2,
            'includeAll': True, 'multi': True, 'allValue': '.*', 'current': {'text': 'All', 'value': '$__all'},
            'sort': 1}


def montar(uid, titulo, paineis, tags, variaveis=None):
    return {'__inputs': [{'name': 'DS_PARQUE', 'label': 'InfluxDB parque', 'description': 'Banco parque do coletor do parque',
                          'type': 'datasource', 'pluginId': 'influxdb', 'pluginName': 'InfluxDB'}],
            '__requires': [{'type': 'datasource', 'id': 'influxdb', 'name': 'InfluxDB', 'version': '1.0.0'}],
            'uid': uid, 'title': titulo, 'tags': tags, 'timezone': 'browser', 'refresh': '5m',
            'time': {'from': 'now-7d', 'to': 'now'}, 'timepicker': {'hidden': uid.endswith('tv')}, 'schemaVersion': 39,
            'editable': True, 'graphTooltip': 0, 'panels': paineis,
            'templating': {'list': variaveis or []}, 'annotations': {'list': []}}


tv = montar('parque-tv', 'Parque - TV', cabecalho('PARQUE · ESTAÇÕES DO DOMÍNIO') + numeros() + [unidades(7, 14), pendencias(7, 14)],
            ['parque', 'tv'])
detalhe = montar('parque-detalhe', 'Parque - Detalhe',
                 cabecalho('PARQUE · MÁQUINA A MÁQUINA') + numeros() + [tabela_detalhe(7)], ['parque', 'ti'],
                 [variavel('unidade', 'Unidade', 'SHOW TAG VALUES FROM "parque_maquina" WITH KEY = "unidade"'),
                  variavel('tipo', 'Tipo', 'SHOW TAG VALUES FROM "parque_maquina" WITH KEY = "tipo"')])

if __name__ == '__main__':
    pasta = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'grafana')
    for nome, d in (('dashboard_fleet_tv.json', tv), ('dashboard_fleet_detail.json', detalhe)):
        with open(os.path.join(pasta, nome), 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print('gerado:', nome, '|', len(d['panels']), 'painéis | altura',
              max(p['gridPos']['y'] + p['gridPos']['h'] for p in d['panels']))
