"""Cliente mínimo (somente leitura) da OpenAPI do Kaspersky Security Center (porta 13299).

Use um usuário interno do KSC com a função Auditor. O KSC usa certificado próprio, por isso a
verificação TLS fica desligada aqui; prefira apontar para o servidor por um nome/IP interno.
"""
import base64
import json

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def plano(v):
    """Tira os envelopes {type, value} que a API usa para datas, longs e params."""
    if isinstance(v, dict):
        if set(v) == {'type', 'value'}:
            return plano(v['value'])
        return {k: plano(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plano(x) for x in v]
    return v


class Ksc:
    def __init__(self, url, usuario, senha):
        self.base = url.rstrip('/') + '/api/v1.0'
        self.s = requests.Session()  # a sessão do KSC fica presa à conexão TCP: usar sempre a mesma
        self.s.verify = False
        b = lambda t: base64.b64encode(t.encode()).decode()
        r = self.s.post(f'{self.base}/login', timeout=60, headers={
            'Authorization': f'KSCBasic user="{b(usuario)}", pass="{b(senha)}", internal="1"',
            'Content-Type': 'application/json'})
        r.raise_for_status()

    def __call__(self, metodo, corpo):
        r = self.s.post(f'{self.base}/{metodo}', data=json.dumps(corpo).encode(), timeout=120,
                        headers={'Content-Type': 'application/json; charset=utf-8'})
        r.raise_for_status()
        j = json.loads(r.content.decode('utf-8'))  # o KSC não manda charset
        if j.get('PxgError'):
            raise RuntimeError(f"{metodo}: {j['PxgError']}")
        return j

    def iterar(self, accessor, total):
        itens = []
        for i in range(0, total, 200):
            c = self('ChunkAccessor.GetItemsChunk', {'strAccessor': accessor, 'nStart': i, 'nCount': 200})
            itens += [plano(x['value']) for x in c['pChunk']['KLCSP_ITERATOR_ARRAY']]
        self('ChunkAccessor.Release', {'strAccessor': accessor})
        return itens

    def fechar(self):
        try:
            self('Session.EndSession', {})
        except Exception:
            pass
        self.s.close()
