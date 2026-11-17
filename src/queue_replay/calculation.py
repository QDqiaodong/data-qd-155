import heapq
from fractions import Fraction

def simulate(servers,jobs):
    heap=[(0,i) for i in range(servers)];heapq.heapify(heap);facts=[];busy=[0]*servers
    for j in sorted(jobs,key=lambda j:(j['arrival'],j['id'])):
        ready,sid=heapq.heappop(heap);start=max(ready,j['arrival']);finish=start+j['service'];busy[sid]+=j['service'];heapq.heappush(heap,(finish,sid))
        facts.append({'id':j['id'],'server':sid,'arrival':j['arrival'],'start':start,'finish':finish,'wait':start-j['arrival'],'service':j['service']})
    first=min((j['arrival'] for j in jobs),default=None);last=max((f['finish'] for f in facts),default=None);span=last-first if first is not None else 0
    return {'jobs':facts,'mean_wait':str(Fraction(sum(f['wait'] for f in facts),len(facts))) if facts else None,'max_wait':max((f['wait'] for f in facts),default=None),'horizon_start':first,'horizon_end':last,'servers':[{'server':s,'busy_ticks':b,'utilization':str(Fraction(b,span)) if span else None} for s,b in enumerate(busy)]}
