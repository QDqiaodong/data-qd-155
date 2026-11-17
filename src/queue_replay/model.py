from .common import require,ident,number,rows
from .calculation import simulate

def analyze(data):
    require(data,'servers jobs');servers=number(data['servers'],1,100);jobs=[];seen=set()
    for j in rows(data['jobs'],20000):
        require(j,'id arrival service');jid=ident(j['id']);arrival=number(j['arrival'],0,10**12);service=number(j['service'],0,10**9)
        if jid in seen:raise ValueError('duplicate job')
        seen.add(jid);jobs.append({'id':jid,'arrival':arrival,'service':service})
    return simulate(servers,jobs)
