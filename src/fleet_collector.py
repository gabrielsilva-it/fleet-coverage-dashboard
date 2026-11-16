#!/usr/bin/env python3
"""Coletor do PARQUE de máquinas -> InfluxDB 1.8 (painéis "Parque - TV" e "Parque - Detalhe" no Grafana).

A BASE é o AD: toda máquina do domínio vira uma linha. Para cada uma junta, pelo nome:
  AD        último logon, sistema, OU, conta ativa
  Tactical  agente, último contato, usuário logado, IP, série, modelo
  GLPI      GLPI Agent (último inventário), estado, usuário, entidade
  Kaspersky agente do KSC, antivírus (KES) e proteção ativa
Máquinas que aparecem nas ferramentas mas NÃO existem no AD entram como "fora do AD" (lixo para limpar).
Tudo é SÓ LEITURA nas quatro fontes.

Configuração em /etc/parque-coletor.conf (chmod 600), formato CHAVE=valor (variáveis de ambiente têm prioridade):
    AD_SERVER=dc01.corp.example.com
    AD_USER=svc-fleet@corp.example.com
    AD_PASS=...
    AD_BASE=DC=corp,DC=example,DC=com
    TACTICAL_URL=https://api.rmm.example.com
    TACTICAL_KEY=...
    GLPI_URL=https://glpi.example.com/apirest.php
    GLPI_APP_TOKEN=...
    GLPI_USER_TOKEN=...
    KSC_URL=https://ksc.example.com:13299   (opcional: sem KSC_URL o antivírus fica fora da cobertura)
    KSC_USER=...                             (usuário interno do KSC com função Auditor)
    KSC_PASS=...
    INFLUX_URL=http://127.0.0.1:8086
    INFLUX_DB=parque
    SITES=HQ,BR01,BR02          (siglas de unidade usadas no nome das máquinas: PREFIXO-SIGLA-NNN)
    SITE_ALIASES=matriz=HQ;filial 1=BR01   (opcional: trecho do nome da entidade do GLPI -> sigla)
    HOST_PREFIX=CORP-           (opcional: prefixo tirado dos nomes nas listas do painel)
    DIAS_ATIVO=30          (logon no AD em até N dias = máquina em uso)
    DIAS_FERRAMENTA=7      (ferramenta "ok" se a máquina falou com ela em até N dias)

Uso:
    parque_coletor.py           coleta e grava
    parque_coletor.py --teste   coleta e mostra o resumo, sem gravar nada
"""
import datetime as dt
import ipaddress
import json
import os
import re
import sys
import urllib3

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONF = '/etc/parque-coletor.conf'
UNIDADES = {'HQ', 'BR01', 'BR02'}      # trocado por SITES da configuração em main()
SITE_ALIASES = ()                       # trocado por SITE_ALIASES da configuração em main()
HOST_PREFIX = ''                        # trocado por HOST_PREFIX da configuração em main()
NAO_PC = {'AZUREADSSOACC'}  # conta do Entra ID que aparece como computador
RTP_OK = {4, 5, 6, 7, 8}


# ---------------------------------------------------------------- utilidades
def config():
    c = {}
    if os.path.exists(CONF):
        for l in open(CONF, encoding='utf-8'):
            l = l.strip()
            if l and not l.startswith('#') and '=' in l:
                k, v = l.split('=', 1)
                c[k.strip()] = v.strip()
    for k in list(c) + ['AD_SERVER', 'AD_USER', 'AD_PASS', 'AD_BASE', 'TACTICAL_URL', 'TACTICAL_KEY', 'GLPI_URL',
                        'GLPI_APP_TOKEN', 'GLPI_USER_TOKEN', 'KSC_URL', 'KSC_USER', 'KSC_PASS', 'INFLUX_URL', 'INFLUX_DB',
                        'SITES', 'SITE_ALIASES', 'HOST_PREFIX', 'DIAS_ATIVO', 'DIAS_FERRAMENTA']:
        if os.environ.get(k):
            c[k] = os.environ[k]
    c.setdefault('INFLUX_URL', 'http://127.0.0.1:8086')
    c.setdefault('INFLUX_DB', 'parque')
    return c


def aplicar_config_unidades(c):
    """SITES, SITE_ALIASES e HOST_PREFIX da configuração viram as regras de unidade e de nome curto."""
    global UNIDADES, SITE_ALIASES, HOST_PREFIX
    if c.get('SITES'):
        UNIDADES = {s.strip().upper() for s in c['SITES'].split(',') if s.strip()}
    SITE_ALIASES = tuple((k.strip().lower(), v.strip().upper()) for k, v in
                         (p.split('=', 1) for p in (c.get('SITE_ALIASES') or '').split(';') if '=' in p))
    HOST_PREFIX = (c.get('HOST_PREFIX') or '').upper()


def nome_padrao(n):
    """CORP-HQ-521.corp.example.com / corp-hq-521 -> CORP-HQ-521"""
    return str(n or '').strip().split('.')[0].upper()


def unidade(nome, entidade=''):
    p = nome.split('-')
    if len(p) >= 3:
        u = p[1].rstrip('0123456789') or p[1]
        if u in UNIDADES:
            return u
        if p[1] in UNIDADES:
            return p[1]
    e = (entidade or '').lower()  # nome fora do padrão: tenta a entidade do GLPI
    for chave, u in SITE_ALIASES:
        if chave in e:
            return u
    return 'OUTROS'


def para_data(v):
    if v in (None, '', 0, '0'):
        return None
    if isinstance(v, dt.datetime):
        d = v
    elif isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit() and len(v) > 12):
        n = int(v)  # FILETIME do AD (100 ns desde 1601)
        if n <= 0:
            return None
        d = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=n / 10)
    else:
        s = str(v).strip().replace('Z', '+00:00')
        try:
            d = dt.datetime.fromisoformat(s.replace(' ', 'T') if 'T' not in s and ' ' in s else s)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone(dt.timedelta(hours=-3)))  # GLPI grava no horário local
    return None if d.year < 2000 else d


def dias(agora, v):
    d = para_data(v)
    return round((agora - d).total_seconds() / 86400, 2) if d else None


def esc(t):
    return str(t).replace('\\', '\\\\').replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')


def linha(medida, tags, campos, ts):
    t = ','.join(f'{esc(k)}={esc(v)}' for k, v in tags.items() if v not in (None, ''))
    f = []
    for k, v in campos.items():
        if v is None:
            continue
        if isinstance(v, bool):
            f.append(f'{k}={"true" if v else "false"}')
        elif isinstance(v, int):
            f.append(f'{k}={v}i')
        elif isinstance(v, float):
            f.append(f'{k}={v}')
        else:
            f.append(f'{k}="{str(v).replace(chr(92), "/").replace(chr(34), chr(39))}"')
    return f'{medida},{t} {",".join(f)} {ts}'


# ---------------------------------------------------------------- fontes
def ler_ad(c):
    from ldap3 import Server, Connection, SIMPLE, ALL, Tls
    import ssl
    tentativas = [dict(port=636, use_ssl=True), dict(port=389, use_ssl=False)]
    ultimo = None
    for t in tentativas:
        try:
            srv = Server(c['AD_SERVER'], get_info=ALL, connect_timeout=10,
                         tls=Tls(validate=ssl.CERT_NONE) if t['use_ssl'] else None, **t)
            con = Connection(srv, user=c['AD_USER'], password=c['AD_PASS'], authentication=SIMPLE,
                             auto_bind=True, receive_timeout=60)
            break
        except Exception as e:
            ultimo = e
            con = None
    if con is None:
        raise RuntimeError(f'AD: não conectou ({ultimo})')
    itens = con.extend.standard.paged_search(
        c['AD_BASE'], '(objectCategory=computer)', paged_size=500, generator=False,
        attributes=['name', 'operatingSystem', 'lastLogonTimestamp', 'distinguishedName', 'userAccountControl', 'whenCreated'])
    out = {}
    for i in itens:
        a = i.get('attributes') or {}
        n = nome_padrao(a.get('name'))
        if not n or n in NAO_PC:
            continue
        dn = str(a.get('distinguishedName') or '')
        uac = a.get('userAccountControl') or 0
        out[n] = {'so': str(a.get('operatingSystem') or ''), 'logon': a.get('lastLogonTimestamp'),
                  'ou': ','.join(x.split('=', 1)[1] for x in dn.split(',')[1:] if x.upper().startswith('OU=')),
                  'habilitado': not (int(uac) & 2), 'criado': a.get('whenCreated')}
    con.unbind()
    return out


def ler_tactical(c):
    r = requests.get(c['TACTICAL_URL'].rstrip('/') + '/agents/', headers={'X-API-KEY': c['TACTICAL_KEY']}, timeout=120)
    r.raise_for_status()
    out = {}
    for a in r.json():
        n = nome_padrao(a.get('hostname'))
        ant = out.get(n)
        if ant and str(ant['visto']) >= str(a.get('last_seen') or ''):
            continue  # entradas duplicadas: fica a mais recente
        ips = [x.strip() for x in str(a.get('local_ips') or '').split(',') if x.strip()]
        ip = next((x for x in ips if x.startswith('10.') or x.startswith('172.') or x.startswith('192.168.')), ips[0] if ips else '')
        out[n] = {'status': a.get('status'), 'visto': a.get('last_seen'), 'usuario': a.get('logged_username') or '',
                  'ip': ip, 'serial': a.get('serial_number') or '', 'modelo': a.get('make_model') or '',
                  'so': a.get('operating_system') or '', 'tipo': a.get('monitoring_type'), 'site': a.get('site_name')}
    return out


def ler_glpi(c):
    base = c['GLPI_URL'].rstrip('/')
    h = {'App-Token': c['GLPI_APP_TOKEN'], 'Content-Type': 'application/json'}
    r = requests.get(base + '/initSession', headers={**h, 'Authorization': f"user_token {c['GLPI_USER_TOKEN']}"}, timeout=60)
    r.raise_for_status()
    h['Session-Token'] = r.json()['session_token']
    out = {}
    try:
        inicio = 0
        while True:
            r = requests.get(base + '/Computer', headers=h, timeout=120,
                             params={'range': f'{inicio}-{inicio + 499}', 'expand_dropdowns': 'true', 'is_deleted': 'false'})
            if r.status_code not in (200, 206):
                r.raise_for_status()
            lote = r.json()
            for x in lote:
                n = nome_padrao(x.get('name'))
                if not n:
                    continue
                ant = out.get(n)
                atual = {'inventario': x.get('last_inventory_update'), 'estado': x.get('states_id') or '',
                         'usuario': (x.get('contact') or '').split('@')[0], 'entidade': x.get('entities_id') or '',
                         'serial': x.get('serial') or '', 'id': x.get('id'), 'cadastros': 1 + (ant['cadastros'] if ant else 0)}
                if ant and str(ant['inventario'] or '') > str(atual['inventario'] or ''):
                    ant['cadastros'] = atual['cadastros']
                    continue  # duplicado: fica o que o agente atualizou
                out[n] = atual
            total = int((r.headers.get('Content-Range') or '0-0/0').split('/')[-1] or 0)
            inicio += 500
            if not lote or inicio >= total:
                break
    finally:
        requests.get(base + '/killSession', headers=h, timeout=30)
    return out


def ler_ksc(c):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from ksc_client import Ksc  # login e paginação da OpenAPI do Kaspersky Security Center
    ksc = Ksc(c['KSC_URL'], c['KSC_USER'], c['KSC_PASS'])
    try:
        h = ksc('HostGroup.FindHosts', {'wstrFilter': '(KLHST_WKS_DN = "*")', 'vecFieldsToOrder': [], 'lMaxLifeTime': 120,
                                        'vecFieldsToReturn': ['KLHST_WKS_DN', 'KLHST_WKS_IP_LONG', 'KLHST_WKS_NAG_VERSION',
                                                              'KLHST_WKS_LAST_NAGENT_CONNECTED', 'KLHST_WKS_RTP_STATE',
                                                              'KLHST_WKS_RTP_AV_VERSION', 'KLHST_WKS_OS_NAME']})
        itens = ksc.iterar(h['strAccessor'], h['PxgRetVal'])
    finally:
        ksc.fechar()
    out = {}
    for x in itens:
        n = nome_padrao(x.get('KLHST_WKS_DN'))
        if not n or not x.get('KLHST_WKS_NAG_VERSION'):
            continue  # registro descoberto pelo AD, sem agente: não conta como "tem Kaspersky"
        ant = out.get(n)
        if ant and str(ant['contato'] or '') >= str(x.get('KLHST_WKS_LAST_NAGENT_CONNECTED') or ''):
            continue
        ip = ''
        try:
            v = int(x.get('KLHST_WKS_IP_LONG') or 0)
            if v:
                ip = str(ipaddress.IPv4Address(v & 0xFFFFFFFF))
        except Exception:
            pass
        rtp = int(x.get('KLHST_WKS_RTP_STATE') or 0)
        out[n] = {'contato': x.get('KLHST_WKS_LAST_NAGENT_CONNECTED'), 'kes': str(x.get('KLHST_WKS_RTP_AV_VERSION') or ''),
                  'protegida': rtp in RTP_OK, 'ip': ip}
    return out


# ---------------------------------------------------------------- junção
def juntar(agora, ad, tac, glpi, ksc, dias_ativo, dias_ferr, usa_av=True):
    nomes = set(ad) | set(tac) | set(glpi) | set(ksc)
    maquinas = []
    for n in sorted(nomes):
        a, t, g, k = ad.get(n), tac.get(n), glpi.get(n), ksc.get(n)
        so = (a or {}).get('so') or (t or {}).get('so') or ''
        servidor = 'server' in so.lower() or 'Servidores' in (a or {}).get('ou', '') or (t or {}).get('tipo') == 'server'
        tipo = 'servidor' if servidor else ('outro' if a and not so and not t and not k else 'estacao')  # NAS etc. sem SO no AD
        m = {'maquina': n, 'unidade': unidade(n, (g or {}).get('entidade', '')), 'tipo': tipo,
             'em_ad': a is not None, 'so': so, 'ou': (a or {}).get('ou', ''),
             'ad_dias': dias(agora, (a or {}).get('logon')) if a else None,
             'tactical_dias': dias(agora, t['visto']) if t else None, 'tactical_status': (t or {}).get('status') or 'ausente',
             'glpi_dias': dias(agora, g['inventario']) if g else None, 'glpi_estado': (g or {}).get('estado', ''),
             'glpi_cadastros': (g or {}).get('cadastros', 0),
             'ksc_dias': dias(agora, k['contato']) if k else None, 'kes': bool(k and k['kes']), 'protegida': bool(k and k['protegida']),
             'usuario': ((t or {}).get('usuario') or (g or {}).get('usuario') or '').split('\\')[-1],
             'ip': (t or {}).get('ip') or (k or {}).get('ip') or '',
             'serial': (t or {}).get('serial') or (g or {}).get('serial') or '', 'modelo': (t or {}).get('modelo') or ''}
        m['ad_ativo'] = bool(a and a['habilitado'] and m['ad_dias'] is not None and m['ad_dias'] <= dias_ativo)
        m['tactical_ok'] = m['tactical_dias'] is not None and m['tactical_dias'] <= dias_ferr
        m['glpi_ok'] = m['glpi_dias'] is not None and m['glpi_dias'] <= dias_ferr
        m['ksc_ok'] = m['kes'] and m['ksc_dias'] is not None and m['ksc_dias'] <= dias_ferr
        exigidas = ((('Kaspersky', m['ksc_ok']),) if usa_av else ()) + (('Tactical', m['tactical_ok']), ('GLPI', m['glpi_ok']))
        falta = [nome for nome, ok in exigidas if not ok]
        m['completo'] = not falta
        m['faltando'] = ', '.join(falta) or '-'
        if not m['em_ad']:
            m['situacao'] = 'Fora do AD'
        elif not m['ad_ativo']:
            m['situacao'] = 'Inativa no AD'
        else:
            m['situacao'] = 'Completa' if not falta else 'Falta ' + ' + '.join(falta)
        maquinas.append(m)
    return maquinas


def curto(n):
    return n[len(HOST_PREFIX):] if HOST_PREFIX and n.startswith(HOST_PREFIX) else n


def resumo(maquinas, usa_av=True):
    est = [m for m in maquinas if m['tipo'] == 'estacao']
    ativas = [m for m in est if m['ad_ativo']]
    g = {'ad_total': sum(m['em_ad'] for m in maquinas), 'ad_estacoes': sum(m['em_ad'] for m in est),
         'ad_servidores': sum(m['em_ad'] for m in maquinas if m['tipo'] == 'servidor'), 'ativas': len(ativas),
         'inativas_31_90': sum(1 for m in est if m['em_ad'] and m['ad_dias'] is not None and 30 < m['ad_dias'] <= 90),
         'inativas_mais_90': sum(1 for m in est if m['em_ad'] and (m['ad_dias'] is None or m['ad_dias'] > 90)),
         'com_kaspersky': sum(m['ksc_ok'] for m in ativas), 'com_tactical': sum(m['tactical_ok'] for m in ativas),
         'com_glpi': sum(m['glpi_ok'] for m in ativas), 'completas': sum(m['completo'] for m in ativas),
         'fora_ad_tactical': sum(1 for m in maquinas if not m['em_ad'] and m['tactical_status'] != 'ausente'),
         'fora_ad_glpi': sum(1 for m in maquinas if not m['em_ad'] and m['glpi_cadastros']),
         'fora_ad_kaspersky': sum(1 for m in maquinas if not m['em_ad'] and m['ksc_dias'] is not None),
         'glpi_duplicados': sum(1 for m in maquinas if m['glpi_cadastros'] > 1)}
    for k in ('com_kaspersky', 'com_tactical', 'com_glpi', 'completas'):
        g['pct_' + k.replace('com_', '')] = round(100.0 * g[k] / g['ativas'], 1) if g['ativas'] else 0.0
    unid = {}
    for m in ativas:
        u = unid.setdefault(m['unidade'], {'ativas': 0, 'com_kaspersky': 0, 'com_tactical': 0, 'com_glpi': 0, 'completas': 0})
        u['ativas'] += 1
        u['com_kaspersky'] += m['ksc_ok']
        u['com_tactical'] += m['tactical_ok']
        u['com_glpi'] += m['glpi_ok']
        u['completas'] += m['completo']
    for u in unid.values():
        u['pct_completas'] = round(100.0 * u['completas'] / u['ativas'], 1)
    pend = ([('Sem Kaspersky', [m for m in ativas if not m['ksc_ok']])] if usa_av else []) + [
            ('Sem Tactical', [m for m in ativas if not m['tactical_ok']]),
            ('Sem GLPI', [m for m in ativas if not m['glpi_ok']]),
            ('Fora do AD (nas ferramentas)', [m for m in maquinas if not m['em_ad'] and (m['tactical_ok'] or m['ksc_ok'] or m['glpi_ok'])]),
            ('Sem logon no AD > 90 dias', [m for m in est if m['em_ad'] and (m['ad_dias'] is None or m['ad_dias'] > 90)])]
    return g, unid, pend


def gravar(c, agora, maquinas, usa_av=True):
    ts = int(agora.timestamp())
    g, unid, pend = resumo(maquinas, usa_av)
    L = []
    for m in maquinas:
        campos = {k: m[k] for k in ('em_ad', 'ad_ativo', 'ad_dias', 'tactical_ok', 'glpi_ok', 'ksc_ok', 'completo',
                                    'tactical_dias', 'glpi_dias', 'ksc_dias', 'protegida', 'glpi_cadastros')}
        campos.update({k: m[k] or '-' for k in ('usuario', 'ip', 'so', 'modelo', 'serial', 'faltando', 'situacao', 'ou',
                                                'tactical_status', 'glpi_estado')})
        L.append(linha('parque_maquina', {'maquina': m['maquina'], 'unidade': m['unidade'], 'tipo': m['tipo']}, campos, ts))
    for u, v in unid.items():
        L.append(linha('parque_unidade', {'unidade': u}, v, ts))
    L.append(linha('parque_geral', {'base': 'AD'}, g, ts))
    for ordem, (motivo, lista) in enumerate(pend, 1):
        nomes = sorted(curto(m['maquina']) for m in lista)
        txt = ' · '.join(nomes[:12]) + (f'  +{len(nomes) - 12}' if len(nomes) > 12 else '')
        L.append(linha('parque_pendencia', {'motivo': motivo}, {'qtd': len(nomes), 'ordem': ordem, 'maquinas': txt or '-'}, ts))
    r = requests.post(f"{c['INFLUX_URL'].rstrip('/')}/write", params={'db': c['INFLUX_DB'], 'precision': 's'},
                      data='\n'.join(L).encode('utf-8'), timeout=60)
    r.raise_for_status()
    return len(L)


def main():
    c = config()
    falta = [k for k in ('AD_SERVER', 'AD_USER', 'AD_PASS', 'AD_BASE', 'TACTICAL_URL', 'TACTICAL_KEY', 'GLPI_URL',
                         'GLPI_APP_TOKEN', 'GLPI_USER_TOKEN') if not c.get(k)]
    if falta:
        sys.exit(f'Faltando na configuração: {", ".join(falta)}')
    aplicar_config_unidades(c)
    usa_av = bool(c.get('KSC_URL'))
    agora = dt.datetime.now(dt.timezone.utc)
    fontes, erros = {'Kaspersky': {}}, {}
    leitores = [('AD', lambda: ler_ad(c)), ('Tactical', lambda: ler_tactical(c)), ('GLPI', lambda: ler_glpi(c))]
    if usa_av:
        leitores.append(('Kaspersky', lambda: ler_ksc(c)))
    for nome, f in leitores:
        try:
            fontes[nome] = f()
        except Exception as e:
            erros[nome] = str(e)[:200]
    if erros:
        msg = '; '.join(f'{k}: {v}' for k, v in erros.items())
        if '--teste' not in sys.argv:  # não grava dado parcial: o painel continua com a última coleta boa
            requests.post(f"{c['INFLUX_URL'].rstrip('/')}/write", params={'db': c['INFLUX_DB'], 'precision': 's'},
                          data=linha('parque_coleta', {'base': 'AD'}, {'ok': False, 'erro': msg}, int(agora.timestamp())).encode(),
                          timeout=30)
        sys.exit(f'Coleta falhou ({msg})')
    maquinas = juntar(agora, fontes['AD'], fontes['Tactical'], fontes['GLPI'], fontes['Kaspersky'],
                      float(c.get('DIAS_ATIVO', 30)), float(c.get('DIAS_FERRAMENTA', 7)), usa_av)
    if '--teste' in sys.argv:
        g, unid, pend = resumo(maquinas, usa_av)
        print('fontes:', ', '.join(f'{k} {len(v)}' for k, v in fontes.items()))
        print('geral:', json.dumps(g, ensure_ascii=False))
        print('unidades:', json.dumps(unid, ensure_ascii=False))
        for motivo, lista in pend:
            print(f'{motivo}: {len(lista)}  ex.: {", ".join(curto(m["maquina"]) for m in lista[:8])}')
        ex = next((m for m in maquinas if m['ad_ativo'] and m['completo']), None)
        if ex:
            print('exemplo:', json.dumps({k: ex[k] for k in ('maquina', 'unidade', 'situacao', 'ad_dias', 'tactical_dias',
                                                             'glpi_dias', 'ksc_dias')}, ensure_ascii=False))
        return
    n = gravar(c, agora, maquinas, usa_av)
    requests.post(f"{c['INFLUX_URL'].rstrip('/')}/write", params={'db': c['INFLUX_DB'], 'precision': 's'},
                  data=linha('parque_coleta', {'base': 'AD'}, {'ok': True, 'erro': '-'}, int(agora.timestamp())).encode(), timeout=30)
    print(f'{agora:%Y-%m-%d %H:%M} gravadas {n} linhas no InfluxDB')


if __name__ == '__main__':
    main()
