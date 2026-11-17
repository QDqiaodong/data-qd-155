# FIFO multi-server queue replay v1

服务运营分析者回放最多20000个唯一作业id、arrival(0..10^12整数tick)、service(0..10^9整数tick)，servers=1..100。按arrival/id排列为明确FIFO到达顺序；同刻按ID打破，不隐含输入行顺序。
每单分给可用时刻最早的服务台，同可用时刻按server编号；start=max(arrival,server_ready)，非抢占服务。若多个服务台在到达前已经空闲，选择空闲最早者。零时长作业合法且仍按上述顺序处理。
输出每单start/finish/wait/server及平均/最长等待，server busy和利用率。观测范围从首到达到最后完成，不把此前空闲计入；无作业/零范围利用率为null。样例A[0,5)台0、B[0,2)台1、C[2,5)台1、D[5,6)台0；平均等待1、最长3，利用率1和5/6。
无随机到达推断、放弃、优先抢占、路由网络、排班窗口或真实服务控制。这是有明确到达数据的离线运营分析，不以泊松或稳态假设代替观测。
