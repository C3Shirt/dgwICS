"""Curated design inputs for the 100-project planning library (not executable scenes)."""

FAMILIES = {
    "transport": ("基础输送与供料", 12),
    "routing": ("转台、移载与分流", 10),
    "buffer_merge": ("缓存与汇流", 12),
    "sorting": ("检测与分拣", 14),
    "handling": ("抓放与上下料", 10),
    "machining": ("加工单元", 8),
    "assembly": ("装配单元", 8),
    "palletizing": ("码垛与托盘物流", 8),
    "warehouse": ("升降与立体仓储", 10),
    "process_control": ("液位、流量与模拟量控制", 8),
}

# Columns: level | slug | title | template group | directed process graph |
# materials | structural/layout decision | controller design | acceptance oracle | repair exercise.
# The process graph links modules, not individual prefabs; observation sensors live inside modules.
ROWS = {
"transport": """
1|single_conveyor|单段输送到位停止|T01|feed1>belt1>exit1|普通箱体|单段直线输送，末端检测线设在移除区之前|空闲、供料、输送、到位、放行；到位上升沿计数|连续10箱各触发一次到位，末端停止后放行才移除|末端光电安装过低导致漏检
1|two_section_handoff|两段独立输送交接|T02|feed1>belt1>belt2>exit1|普通箱体|两段独立电机与交界检测，接触面共高|下游就绪才启动上游；交界占用后维持接货|10次交接无跨缝掉落，堵住第二段时第一段不再供新箱|交接缝隙过大导致卡箱
1|three_section_line|三段输送逐段握手|T02|feed1>belt1>belt2>belt3>exit1|普通箱体|三段串联，每段有独立占用检测|相邻段请求、允许、完成握手；下游向上游反压|10箱按进入顺序离开，第三段阻塞使前两段逐级停料|中间段方向反转
1|curved_corner|弯带转角输送|T03|feed1>belt1>curve1>belt2>exit1|普通箱体|直带与弯带切线衔接，内侧留导向空间|弯带被占用时禁止新箱进入；三电机分段控制|左右边缘起始偏置各5箱均完成转角，不落地|弯带朝向与入口不相切
1|inclined_transfer|斜坡升高输送|T04|feed1>belt1>incline1>belt2>exit1|普通箱体|低位带、斜带、高位带；按坡端接触面确定高度|入口许可与坡顶清空互锁；停止后不重复计数|10箱到达高位，坡顶阻塞时无叠箱或落箱|高位带接触面高于坡顶
1|reversible_shuttle|往返输送与双端检测|T01|feed1>reverse1>exit1|普通箱体|双向带两端检测线均在带面安全区内|先正向到右端、停稳、反向回左端、再送出；方向互斥|10箱完成右端及左端各一次确认，两方向命令不同时为真|反向命令和末端移除区冲突
1|heavy_roller_line|托盘滚筒输送|T01|pfeed1>roller1>exit1|空托盘|重载滚筒配托盘，进出端增加跨宽检测|单托盘供料，滚动到位后释放；占用逻辑按极性归一|10托盘全部在滚筒上通行，停止时无穿过挡停区|托盘生成高度过高引起弹跳
1|conveyor_gate|带通行门的输送段|T05|feed1>belt1>gate1>belt2>exit1|普通箱体|通行门前留安全停箱距离并布置检测|门打开时禁止两侧向门输送；关门后重新请求启动|5次开门操作中无箱进入断开的带面，关门后完成剩余10箱|忽略门开信号持续送箱
2|gap_before_scale|称重前双段拉距供料|T02|feed1>abelt1>abelt2>scale1>exit1|三种尺寸普通箱体|两段模拟量带与独立检测建立间距，秤台只容纳一箱|前段限流、后段拉距；测重窗口内不得放第二箱|12箱每次测重窗口只有一个箱，离秤计数与输入一致|传感器位置使第二箱过早进入秤台
2|zigzag_feed|双弯带蛇形供料|T03|feed1>belt1>curve1>belt2>curve2>belt3>exit1|普通箱体|两个转角与中间直带形成蛇形路径，不靠整体旋转充数|两个弯带独立占用锁，瓶颈向前段反压|12箱在两个弯口均无相撞，出口保持FIFO|第二弯口转向选型错误
2|incline_gate_line|升高输送与高位通行门|T04|feed1>incline1>belt1>gate1>belt2>exit1|普通箱体|坡顶后设置停车段，门不放在斜坡上|门开时先停新供料并清空坡顶安全区，再锁定输送|门开关各3次，坡上和停车段的箱均可按记录继续送出|坡顶停车段长度不足
3|dual_feed_gap_line|双源汇入的拉距供料线|B02|feed1>belt1>merge1>abelt3>scale1>exit1,feed2>belt2>merge1|普通箱体|两条入口汇入共用拉距段和秤台|入口公平仲裁，秤台单件锁；多个在途项关联队列|两源各10箱均获得服务，20个独立称重记录与出口逐项对应|仲裁释放和秤台忙碌同时失效
""",
"routing": """
1|pallet_turntable|托盘转台九十度移载|R01|pfeed1>roller1>turn1>roller2>exit1|空托盘|转台入口与垂直出口在同一接触面，旋转扫掠区留空|接货、停辊、旋转、到角、出货、回零；转动时不进货|10托盘均在角度确认后出货，无边缘滑落|忽略转台到角反馈
1|pallet_chain_transfer|托盘链式横向移载|R02|pfeed1>roller1>chain1>roller2>exit1|空托盘|入口滚筒与横向出口按链条方向对接|进货到位、横移、出口确认、回待机；左右互斥|10托盘完整横移，横移过程中入口停止供料|链式横移左右方向绑定反了
1|wheel_two_outlets|摆轮两出口分流|R03|feed1>belt1>wheel1>belt2>exit1,wheel1>belt3>exit2|普通箱体|摆轮单元连接直行与侧向两出口，分别检测|按预置交替订单锁定路线直到对应出口确认|左右路线各5箱，逐项出口与预置订单一致|只写转向标签未使能摆轮抬升和电机
2|wheel_three_outlets|摆轮三出口分流|R03|feed1>belt1>wheel1>belt2>exit1,wheel1>belt3>exit2,wheel1>belt4>exit3|普通箱体|左、前、右出口分别配置接货带与检测|三路目标锁存，出口不可用就上游等待|三路各4箱独立确认，目标堵塞时不偷换路线|前方出口带面与摆轮存在高差
2|turntable_chain_cell|转台与链移串联|R02|pfeed1>roller1>turn1>roller2>chain1>roller3>exit1|空托盘|先转向再横移，两个运动包络不重叠|转台与链移各自资源锁，交接采用双向握手|12托盘经历两种移载，链移忙时转台不会向外推出托盘|两个工位完成位未清零
2|two_stage_diverter|串联推杆的三级分流|R04|feed1>belt1>push1>belt2>push2>exit1,push1>belt3>exit2,push2>belt4>exit3|普通箱体|第一推杆分支A，第二推杆分支B，主线末端C|预置类别随物料位置推进；第一分支卸出后不再进入第二队列|三类各4箱，第二分流不得使用上一箱类别|推杆伸出时间不足
2|turntable_dual_dispatch|转台直行与侧向托盘派送|R01|pfeed1>roller1>turn1>roller2>exit1,turn1>roller3>exit2|空托盘|转台零度和九十度各有独立出口|对目标角预定位，托盘完整上台后才允许旋转|两出口各6托盘，满出口只阻塞自己的订单|转台中途切换订单
3|parallel_transfer_cells|两条链移通道的汇流|R02|pfeed1>roller1>chain1>roller3>merge1>roller5>exit1,pfeed2>roller2>chain2>roller4>merge1|空托盘|两个横移单元接到共用重载出口，汇流模块选重载版本|两单元独立完成，共用出口仲裁，禁止双向同时卸货|两源各10托盘，出口无相撞且不会长期饿死一源|共用出口占用释放过早
3|route_buffer_remerge|分流缓存后重新汇流|R04|feed1>wheel1>zone1>merge1>belt1>exit1,wheel1>zone2>merge1|普通箱体|两支路各独立缓存，再在单一冲突点回主线|先锁支路再申请汇流；出口忙时缓存保持占用|20箱两支路均被使用，汇流时每箱仅出库一次|支路释放后分类队列仍保留旧箱
4|pallet_route_bypass|托盘移载线与维护旁路|R04|pfeed1>turn1>chain1>roller1>merge1>exit1,turn1>roller2>merge1|空托盘|主路经链移，旁路经独立滚筒，转台负责选路|设备停用时新托盘走旁路，在途托盘先完成或进入待处理状态|主路停用前后共20托盘账实一致，不把链移中的托盘凭空迁到旁路|链移故障后错误清空在途记录
""",
"buffer_merge": """
1|two_zone_fifo|两区串联缓存|B01|feed1>zone1>zone2>exit1|普通箱体|两个挡停区独立电机、占用检测与挡停器|后区空才释放前区，入库与出库分别计数|容量2时第三箱禁止进入，出库保持FIFO|挡停器极性取反
1|three_zone_fifo|三区串联缓存|B01|feed1>zone1>zone2>zone3>exit1|普通箱体|三个实物区段，长度按最大箱体和停车距离校准|逐区推进及反压，禁止仅用总计数判断空位|容量3满空各3轮，区段占用与可见箱数一致|中间区传感器漏检
2|two_to_one_merge|两路输送汇流|B02|feed1>belt1>merge1>belt3>exit1,feed2>belt2>merge1|普通箱体|两入口导入同一主线，冲突区完整覆盖检测|轮询仲裁，进入冲突区到尾部离开期间独占|两源各8箱，在共同区没有重叠通行|汇流占用检测仅覆盖箱头
2|three_to_one_merge|三路输送汇流|B02|feed1>belt1>merge1>belt4>exit1,feed2>belt2>merge1,feed3>belt3>merge1|普通箱体|三个汇入口错开或有明确冲突区，配导入输送|三源公平队列与单一授权，出口阻塞反压所有源|三源各6箱，每源等待次数有限且总数18|两入口同时收到允许
2|batch_accumulator|四区批量放行缓存|B01|feed1>zone1>zone2>zone3>zone4>exit1|普通箱体|四个物理停箱位，末区有独立出库门控|凑齐4箱后逐区出库；未满批等待新料|3个完整批次共12箱，每批实际4箱；半批不误放|出库计数复位时刻错误
2|split_buffers|双支路缓存与独立出库|B04|feed1>wheel1>zone1>zone2>exit1,wheel1>zone3>zone4>exit2|普通箱体|分流到两条各2区缓存，不共用末端挡停|支路容量预留与订单锁；A满时B仍可服务|两路各8箱，堵A后B继续且A容量不超限|分流决策未预留目标空位
3|merge_with_entry_buffers|双源带入口缓存的汇流|B02|feed1>zone1>zone2>merge1>zone5>exit1,feed2>zone3>zone4>merge1|普通箱体|双入口各2区，共用出口1区|入口缓存局部控制，汇流调度读取真实占用|两源各10箱，出口暂堵时无相撞、无账面负数|缓存计数与检测边沿不同步
3|three_buffer_branches|三支路缓存回主线|B04|feed1>wheel1>zone1>merge1>zone4>exit1,wheel1>zone2>merge1,wheel1>zone3>merge1|普通箱体|三路各1区缓冲加公共出库区|分流预留、支路FIFO、回流仲裁三层握手|每支路8箱，24箱只出库一次，满支路禁止再接箱|回流仲裁选择了空支路
3|recirculation_loop|带回流环的等待缓存|B03|feed1>merge1>belt1>wheel1>exit1,wheel1>curve1>belt2>curve2>merge1|普通箱体|有实物闭环与出口分流，回入口和新料争用同一合流点|回流件优先但限制循环次数；在途令牌不因绕圈重复新增|20箱经历临时关出口后全部释放，循环不增加物料总数|把每次经过入口都计为新箱
3|buffer_to_two_cells|共享缓存供应两个工位|B04|feed1>zone1>zone2>zone3>wheel1>belt1>exit1,wheel1>belt2>exit2|普通箱体|三区公共FIFO接双出口，各出口有独立占用|下游需求请求驱动出库；出库承诺后冻结目标|两站请求各10次，实际各到10箱，同一箱不响应两站|请求保持为真导致重复派料
4|buffer_loop_recovery|回流环加应急停车支路|B03|feed1>merge1>zone1>wheel1>exit1,wheel1>curve1>zone2>curve2>merge1,wheel1>zone3>exit2|普通箱体|闭环之外增加可独立隔离的停车支路|达到循环上限转应急支路；恢复时显式确认滞留库存|正常20箱和5个滞留件均有最终去向，恢复不重复计数|循环上限后没有停车位仍继续发料
4|four_source_merge|四源分级缓存汇流|B02|feed1>zone1>merge1>zone5>merge3>exit1,feed2>zone2>merge1,feed3>zone3>merge2>zone6>merge3,feed4>zone4>merge2|普通箱体|四入口先两两汇流，再二级合流，各级有隔离缓存|分层授权与有界公平，禁止下级持锁等待上级造成死锁|四源各10箱，阻塞恢复后40箱全部出库，无无限等待|上下级授权形成循环等待
""",
"sorting": """
1|height_two_way|双光电高度二路分拣|S01|feed1>height1>push1>exit1,push1>belt1>exit2|大小普通箱体|低高两条光路加推杆侧出口，观测窗先于推杆|低位到料后采高位状态，类别锁存到出口|两类各5箱，逐箱路线正确，不用出口计数反推类别|高位光电高于所有箱体
1|metal_two_way|金属与塑料二路分拣|S03|rfeed1>metal1>push1>exit1,push1>belt1>exit2|金属及蓝色塑料Raw Material|电感检测贴近材料，侧向推杆与两出口|占用确认后读取金属位；清空分类后才收下一件|金属塑料各5件均按已知投料类别分流|电感感应距离超限
2|height_three_way|光栅高度三路分拣|S01|feed1>array1>wheel1>belt1>exit1,wheel1>belt2>exit2,wheel1>belt3>exit3|三种尺寸普通箱体|光栅覆盖箱高且发射接收对齐，三出口|光束组合归一化后分类，类别跟踪到摆轮|三类各4箱，临界遮挡另测，错分率0|光栅位序当作数值直接比较
2|weight_two_way|称重二路分拣基线重建|S02|feed1>scale1>push1>belt1>exit1,push1>belt2>exit2|轻重普通箱体|单件秤台、入口隔离、推杆二出口；复用现有基线作对照|稳定采样、重量阈值、到位推料；浮点量程按导出模式校准|轻重各6箱独立核对路线，秤台未满载前不采样|原先箱未出秤就覆盖重量
2|weight_three_way|称重三路摆轮分拣|S02|feed1>scale1>wheel1>belt1>exit1,wheel1>belt2>exit2,wheel1>belt3>exit3|三种尺寸普通箱体|秤台下游接三路摆轮而非二路推杆|两阈值与异常范围检查，三路线出口确认|三类各4箱加两次边界测量，无漏路和二义类别|重量量程选择与PLC换算不一致
2|color_three_way|三色原料视觉分拣|S03|rfeed1>vision1>wheel1>belt1>exit1,wheel1>belt2>exit2,wheel1>belt3>exit3|蓝绿金属Raw Material|相机检测站先于分流点，三路料道|视觉类别仅用于支持的原料类型；一次分类对应一次到料|三种材料各4件，类别结果与独立投料表一致|把普通纸箱当作支持的颜色原料
2|rfid_two_routes|RFID托盘二路订单派送|S04|pfeed1>rfid1>turn1>roller1>exit1,turn1>roller2>exit2|带可写数据的托盘|读写区单托盘停靠，转台零度与九十度两个出口|先预写测试订单再读命令握手；订单来自RFID字段|两类订单各6托盘，读取完成后才派送，读失败停靠|RFID执行握手未等待完成
3|height_weight_matrix|高度与重量联合四出口分拣|S05|feed1>array1>scale1>push1>wheel1>belt1>exit1,push1>belt2>exit2,wheel1>belt3>exit3,wheel1>belt4>exit4|三种普通箱体及受控无法分类测试件|两个检测站串联，三正常类加隔离共四出口；不要求完整高度重量笛卡尔积|双特征关联同一物料ID；异常组合送隔离，不伪造独立高度重量组合|三正常类各5箱及3异常测试件，双特征不会跨箱配对|两个测量队列长度错位
3|color_with_reject|三色分拣加无效品剔除|S03|rfeed1>vision1>push1>wheel1>belt1>exit1,push1>belt2>exit2,wheel1>belt3>exit3,wheel1>belt4>exit4|蓝绿金属原料及不支持类型测试件|先推杆剔除无有效分类件，余件三路色别分流|无效检测与正常类别独立判定，拒绝件占用剔除通道|三类各5件加3无效件，正常15件与拒绝3件分账|无效值沿用上一件视觉类别
3|metal_color_branch|材质优先的双级分拣|S05|rfeed1>metal1>push1>vision1>wheel1>exit1,push1>belt1>exit2,wheel1>belt2>exit3|金属蓝绿Raw Material|金属支路先卸出，塑料支路再分蓝绿|材质检测先决定是否进入颜色站，分级队列交接|金属蓝绿各6件，金属不会占用塑料色别队列|第一级剔除后第二级仍新增记录
3|rfid_weight_dispatch|RFID订单与称重校验派送|S04|pfeed1>rfid1>scale1>turn1>roller1>exit1,turn1>roller2>exit2|空托盘或重量在量程内的载托盘|RFID站与适配量程秤台相邻，重载出口两路|订单决定出口，重量仅做一致性校验；异常原地等待人工处理|两目的地各8托盘，错重测试停靠且不会靠重量猜目的地|订单字段与重量类别混为同一个枚举
4|dual_source_multisort|双源多特征分拣与缓存|S05|feed1>zone1>merge1>array1>scale1>wheel1>zone3>exit1,feed2>zone2>merge1,wheel1>zone4>exit2,wheel1>zone5>exit3|三种普通箱体|双源汇流后集中检测，三出口各有停车缓存|汇流令牌、双检测关联、出口预留协同；满支路等待|两源各15箱，30个记录逐项核对；停一出口后恢复账实一致|满出口仍允许摆轮放行
4|reinspection_loop|称重复检回流与异常出口|S02|feed1>merge1>scale1>wheel1>exit1,wheel1>curve1>belt1>curve2>merge1,wheel1>zone1>exit2|普通箱体；测量不稳定由受控故障用例触发|合格出口、复检回路、异常停车出口三路|每件测量次数上限；复检不新增ID，不靠假设第二次必合格|正常15箱与5个受控异常箱全部有明确去向，循环次数有界|复检计数在回入口被重置
4|parcel_dispatch_cells|订单派送三路加重量异常隔离|S04|feed1>rfid1>scale1>push1>wheel1>zone1>exit1,push1>zone2>exit2,wheel1>zone3>exit3,wheel1>zone4>exit4|可RFID读写的普通箱体，订单测试数据另行预写|重量异常先剔除，其余按订单进入三条缓存发运道|订单与测量分字段记录，三路容量预约，通信失败不派送|三订单各10箱另加3异常件，按独立订单表核对去向与库存|读取失败后使用陈旧订单
""",
"handling": """
1|two_axis_pick|二轴单件抓放|H01|rfeed1>belt1>pick1>belt2>exit1|可抓取Raw Material|取料和放料两个定位点都在二轴运动可达平面内|到料、夹取、抬升、横移、下降、释放、回原位|10件各完成一次抓放，无空抓、拖拽或掉件|夹爪下降位置低于带面
2|two_axis_dual_feed|二轴抓放供料与输出挡停|H01|rfeed1>zone1>pick1>zone2>exit1|可抓取Raw Material|取料位和放料位各增挡停及独立检测|夹取需取料定位完成，释放需目标区空|12件搬运，阻塞目标时夹爪安全保持或回指定停车状态|目标占用仍释放物料
2|xyz_three_targets|XYZ抓放到三目标区|H02|rfeed1>belt1>xyz1>zone1>exit1,xyz1>zone2>exit2,xyz1>zone3>exit3|可抓取Raw Material|三个目标坐标不同且均在XYZ工作包络内|模拟量坐标表、轴到位容差与夹持确认；按外部任务选目标|三目标各4件，落点位于各目标有效接触面|把世界坐标直接当作电压设定值
2|xyz_transfer_turn|XYZ跨垂直料道搬运|H02|rfeed1>belt1>xyz1>belt2>exit1|可抓取Raw Material|两条料道互相垂直且有间隔，取放点不共线|先垂直抬到安全高度再平移，下降前锁定目标|12件跨料道搬运，水平移动时夹持件不碰设备|未抬高就横向移动
2|pick_inspection_cell|抓放后视觉检测|H01|rfeed1>pick1>vision1>belt1>exit1|蓝绿金属Raw Material|取料位、检测定位位、下游输送三个区域|抓放完成交检测令牌，视觉完成才释放下游|三材质各4件，检测记录与抓放次数逐项匹配|抓放完成信号早于释放
3|xyz_two_sources|双取料位共享XYZ|H03|rfeed1>zone1>xyz1>zone3>exit1,rfeed2>zone2>xyz1|可抓取Raw Material|两个供料停靠位共享一个夹爪，一个放料区|取料预约与公平调度，机械运动期间不得更换任务|双源各10件，取放记录与源别一致，无饥饿|请求覆盖正在执行的取料坐标
3|xyz_sort_bins|视觉引导XYZ三箱分料|H02|rfeed1>vision1>xyz1>zone1>exit1,xyz1>zone2>exit2,xyz1>zone3>exit3|蓝绿金属Raw Material|检测点加三实物接料区，夹爪替代摆轮|视觉分类后选坐标，箱区满则等待并关闭供料|三类各6件，空抓不记成功，类别与落区一致|使用上一件视觉结果选坐标
3|robot_twin_machining|共享XYZ给双加工入口配料|H03|rfeed1>zone1>xyz1>machine1>exit1,xyz1>machine2>exit2|Raw Material加工为Base或Lid|双加工入口都在抓放包络内，原站内机器人作为站内行为|XYZ只负责入口配料，机床Busy与入口空握手；站内动作独立|两机各8件，忙机不再次装料，产品类型和数量一致|将加工站自带机器人与外部夹爪占用混淆
4|robot_dual_output_recovery|双源双目标共享机器人及恢复位|H03|rfeed1>zone1>xyz1>zone3>exit1,rfeed2>zone2>xyz1,xyz1>zone4>exit2,xyz1>zone5>exit3|可抓取Raw Material|双取料双正常放料位加一个实物异常停车位|任务包含源、目标、夹持状态；中断后按实际夹持状态重建|两源各12件加3次中断，24件去向完整，恢复不会重复夹取|中断时夹持状态被清空
4|robot_order_kitting|双机器人分区配套供料|H03|rfeed1>zone1>xyz1>zone3>exit1,rfeed2>zone2>xyz2>zone4>exit2|蓝绿Raw Material配对任务|两个不重叠机器人包络，各自交付一个配套料位；配套只做物流抽象|双任务按订单配对，交付位双就绪后逻辑确认套件|10对订单共20件，缺一件不确认套件，包络之间无碰撞|单边交付就增加套件完成计数
""",
"machining": """
2|single_base_machine|单加工中心制造底座|M01|rfeed1>belt1>machine1>belt2>exit1|Raw Material到Base|入口检测、加工中心、出口输送独立布置|设ProduceLids为假，启动与Busy、Error握手，完成后出料|12原料得到12底座，入口错误件不得计正常产品|把Busy下降沿误当无条件成功
2|single_lid_machine_buffer|带出口缓存的盖件加工|M01|rfeed1>belt1>machine1>zone1>exit1|Raw Material到Lid|盖件机床后增加挡停缓存，入口单件隔离|设置盖件模式，出口空才许可下周期；门开阻止启动|12盖件正确产出，堵出口时不继续把产品推入满位|盖件模式绑定到错误输出
3|parallel_base_machines|双加工中心底座并行生产|M02|rfeed1>wheel1>machine1>merge1>exit1,wheel1>machine2>merge1|Raw Material到Base|分流到两机入口，产品出口共用汇流|空闲机预约，机床各自握手，汇流令牌独占|20底座总数正确，两机各承担任务且不向忙机投料|分流时未预留机床入口
3|lid_base_split_machine|盖底两条专用加工线|M02|rfeed1>wheel1>machine1>zone1>exit1,wheel1>machine2>zone2>exit2|Raw Material分别到Lid和Base|两加工中心分别固定盖件和底座模式，出口分开缓存|按配套需求投料，分别维护盖底库存及物料来源|盖底各10件，分别出库20件且一对配额无过量|共用一个库存计数掩盖盖底失衡
3|machining_visual_qc|加工后类型检测与剔除|M03|rfeed1>machine1>vision1>push1>zone1>exit1,push1>zone2>exit2|Raw Material到Base；错类型用受控测试件验证|加工后视觉检测站加正常和异常停车出口|Busy完成后检测输出类型，类型不符走拒绝支路|正常12件加3错类型测试件，正常与拒绝分账|Error复位后把未加工件计为底座
4|twin_machine_rework|双机加工及受控返料支路|M03|rfeed1>wheel1>machine1>vision1>push1>merge1>exit1,wheel1>machine2>vision2>push2>merge1,push1>zone1>exit2,push2>zone2>exit3|Raw Material到Base；异常产品进入隔离而不强行再加工|两机各有检测及隔离位，合格品共用汇流|单机故障隔离后其他机继续；返料只表示人工处置，不假设机床可重加工成品|24任务中每项有合格或隔离结果，单机停用时另一机仍服务|将Base作为Raw Material重新送机
4|three_machine_scheduler|三加工中心共享供料与成品缓存|M02|rfeed1>wheel1>machine1>merge1>zone1>exit1,wheel1>machine2>merge1,wheel1>machine3>merge1|Raw Material到Base|三加工入口与公共成品缓冲，分流能力覆盖三路|三机资源预约、出口背压、停用机剔除调度集合|30件加工；关停一机时剩余两机完成可接任务，无滞留令牌|停用机任务仍持有上游许可
4|machine_order_warehouse|订单加工后托箱入库接口|M03|rfeed1>machine1>vision1>xyz1>boxdock1>roller1>exit1,sfeed1>boxdock1|Raw Material到Base，放入Stackable Box承载|加工成品由XYZ装入可承载底座的箱，空箱单独供给|订单绑定加工件和承载箱，箱可接货才释放机床；只输出入库就绪|12加工件进入对应12承载箱，禁止把裸底座直接当托盘货物|载具未到位就释放成品
""",
"assembly": """
2|base_lid_assembly|双料道盖底装配|A01|bfeed1>zone1>assemble1>belt1>exit1,lfeed1>zone2>assemble1|匹配Base与Lid到Final Product|两条料道定位，底座定位条与盖件夹爪在可达位置|底座就位且盖到位才夹取装配，成功后成品释放|10套产生10实际成品，库存满足1底加1盖等于1成品|定位条未对齐使盖落偏
2|assembly_color_check|颜色核对后盖底装配|A01|bfeed1>vision1>zone1>assemble1>exit1,lfeed1>vision2>zone2>assemble1|同色Base与Lid到Final Product|两路分别视觉识别与挡停，夹爪不兼作检测器|颜色相同才允许成套；不同色等待人工换料而非伪装成功|同色10对装配成功，2对异色不装配也不计成品|两个输入颜色记录来自同一视觉信号
3|assembly_buffered_pair|双缓存料道配套装配|A01|bfeed1>zone1>zone2>assemble1>zone5>exit1,lfeed1>zone3>zone4>assemble1|Base与Lid到Final Product|盖底各2区FIFO，成品1区，装配固定定位条|分别管理部件库存，先预约一盖一底再执行机械步骤|12套配料在不均匀到达情况下仍一一消耗，无单边空装|单边库存耗尽仍启动装配
3|parallel_assembly_cells|两套独立装配单元共用成品出口|A02|bfeed1>zone1>assemble1>merge1>exit1,lfeed1>zone2>assemble1,bfeed2>zone3>assemble2>merge1,lfeed2>zone4>assemble2|Base与Lid到Final Product|两套盖底定位及二轴夹爪，成品汇流处另设检测|各单元独立配套，公共出口请求授权后出料|两站各10套，20成品汇流无相撞；一站缺盖不锁死另一站|两个装配站共用一个步骤变量
3|machine_to_assembly|盖底加工衔接单装配站|A03|rfeed1>machine1>zone1>assemble1>exit1,rfeed2>machine2>zone2>assemble1|两路Raw Material到Base和Lid再到Final Product|专用底座机与盖件机，出口定位条接装配料道|按配套需求启动两机，产速不等靠缓存同步|24原料形成12成品，盖底余量与在制品单独核算|固定等时启动导致慢盖件线缺料
4|assembly_quality_reject|双装配单元及成品外观类型复核|A02|bfeed1>assemble1>merge1>vision1>push1>exit1,lfeed1>assemble1,bfeed2>assemble2>merge1,lfeed2>assemble2,push1>zone1>exit2|Base与Lid到Final Product；类型检测能力待确认|双装配合流后检测与剔除；若视觉不支持成品则用截图人工标注门禁|两站装配握手，成品状态用可验证通道复核，错误件隔离|20次装配逐项有视觉证据；检测能力不足保持待确认，不伪造质量通过|把单个盖件当作已装配成品
4|full_manufacture_assemble|四机双装配连续生产|A03|rfeed1>wheel1>machine1>zone1>assemble1>merge1>exit1,wheel1>machine2>zone2>assemble1,rfeed2>wheel2>machine3>zone3>assemble2>merge1,wheel2>machine4>zone4>assemble2|Raw Material到盖底再到Final Product|每装配站配专用盖底两机，共用成品出口|加工配额、部件库存、装配预约、出库仲裁分层；一线停用另一线保持服务|40原料形成20成品，逐线账实一致，停用恢复无重复消耗|复位只清PLC库存未核对现场物料
4|assembly_carrier_dispatch|装配后装箱与订单派送|A03|bfeed1>assemble1>xyz1>boxdock1>rfid1>turn1>roller1>exit1,lfeed1>assemble1,sfeed1>boxdock1,turn1>roller2>exit2|Base和Lid到Final Product再入Stackable Box；成品承载待试验|装配、XYZ装箱、承载箱RFID和转台派送串联|套件完成后绑定承载箱订单；装箱与RFID成功后派送，承载不支持则调整载具|12成品均有装配、装箱和目的地证据；未验证承载之前保持未验收|把Final Product从物体库直接生成
""",
"palletizing": """
1|builtin_palletizer|内置码垛机单元|P01|pfbox1>belt1>palletizer1>roller1>exit1,pfeed1>palletizer1|Palletizing Box与空托盘|箱料入口和空托盘入口分开，内置码垛机保持默认可行布局|码垛站原生握手，满垛才出托盘，托盘缺料停止接箱|完成2托盘默认垛型，箱数由站配置与图像实数双核对|使用普通箱体代替Palletizing Box
2|xyz_pallet_single|XYZ单托盘逐格摆箱|P02|pfbox1>zone1>xyz1>pdock1>roller1>exit1,pfeed1>pdock1|Palletizing Box与托盘|一个托盘停靠位，拟定2乘2单层坐标表待校准|箱到位、托盘锁定、逐格抓放；满4箱才释放托盘|3托盘各4箱，每格最多1箱，托盘出库前夹爪回安全位|抓放坐标未扣除箱体尺寸
2|palletizer_entry_buffer|带箱缓存的内置码垛单元|P01|pfbox1>zone1>zone2>palletizer1>zone3>exit1,pfeed1>roller1>palletizer1|Palletizing Box与托盘|码垛前2箱缓存，后端托盘缓存采用重载版|箱缓存反压与空托盘握手，满托盘出库条件独立|连续3托盘，暂缺空托盘时箱只占已定义缓存位|空托盘检测与满托盘检测混用
3|xyz_pallet_two_levels|XYZ双层码垛与托盘交换|P02|pfbox1>zone1>xyz1>pdock1>chain1>exit1,pfeed1>roller1>pdock1|Palletizing Box与托盘|2乘2两层共8箱拟定布局，满托盘经链移出库|层高由实际箱高标定，夹爪抬升避开既有垛层；托盘交换冻结坐标表|3托盘各8箱，无箱间穿插，出库时垛型保持稳定|第二层高度仍按第一层计算
3|dual_pallet_docks|一个XYZ服务双托盘位|P02|pfbox1>zone1>xyz1>pdock1>exit1,xyz1>pdock2>exit2,pfeed1>roller1>pdock1,pfeed2>roller2>pdock2|Palletizing Box与托盘|两托盘位在同一XYZ包络内，各自有到位和出库带|各托盘独立格位占用，夹爪共享；换盘时仅锁对应托盘位|两位各2托盘各4箱，A换盘时B可继续，格位记录不串盘|托盘ID更新后保留旧格位数
3|two_palletizers_merge|双码垛机托盘汇流|P03|pfbox1>palletizer1>roller1>merge1>exit1,pfeed1>palletizer1,pfbox2>palletizer2>roller2>merge1,pfeed2>palletizer2|Palletizing Box与托盘|两独立码垛机满托盘进入重载公共汇流|站内握手独立，满托盘出口仲裁，汇流占用覆盖完整托盘|两机各2满托盘，公共出口不碰撞，不按箱计数当托盘数|汇流释放只检测到托盘前沿
4|pallet_pool_three_lines|三箱源共享双码垛站|P03|pfbox1>zone1>merge1>wheel1>palletizer1>merge2>exit1,pfbox2>zone2>merge1,pfbox3>zone3>merge1,wheel1>palletizer2>merge2,pfeed1>palletizer1,pfeed2>palletizer2|Palletizing Box与托盘|三供箱缓存、公共分流、双码垛站及满托盘汇流|供料仲裁、站能力预约、满托盘出库分层；封膜仅预留不模拟物理封装|两站各3托盘；缺一个空盘源时不向该站继续派箱|站忙后仍锁住整条供箱主线
4|rfid_pallet_outbound|双托盘位码垛后RFID发运|P03|pfbox1>xyz1>pdock1>merge1>rfid1>turn1>roller3>exit1,xyz1>pdock2>merge1,pfeed1>roller1>pdock1,pfeed2>roller2>pdock2,turn1>roller4>exit2|Palletizing Box、托盘、预写订单|双托盘位与公共读写发运口，读区一次只容一托盘|格位完成后锁订单与托盘，读写成功再路由；恢复核对垛层实物|4个满托盘订单与两出口对应，读失败不丢失托盘任务|扫描区同时进入两托盘
""",
"warehouse": """
1|two_floor_elevator|两层货物升降交接|W01|pfeed1>roller1>lift1>roller2>exit1|空托盘|底层入口和上层出口按升降平台端点标定|平台空闲且到入口层才接货，到目标层且停稳才出货|10托盘从下层到上层，无悬空卸货或运动中进货|使用位置电压但未换算楼层标高
2|three_floor_elevator|三层选层升降机|W01|pfeed1>roller1>lift1>roller2>exit1,lift1>roller3>exit2|空托盘|底层入口与两个不同标高出口，使用模拟定位|预置目标层任务、位置容差判断、到层卸货握手|中上层各6托盘，目标层占用时平台不卸货|楼层选择坐标表颠倒
2|one_rack_storage|单货架堆垛机存取|W02|pfeed1>load1>crane1>rack1,crane1>load2>exit1|适合叉取的托盘|单货架18位置候选，两个装卸端和叉取姿态待校准|入库预约空位、放货确认提交；出库查已占位，取货确认清位|6托盘入库再按指定顺序出库，货位记录与图像一致|放货开始就把货位设为已占
3|elevator_buffered|升降机双端缓存|W01|pfeed1>zone1>lift1>zone2>exit1|空托盘|两层端各独立重载挡停缓存，升降不替代缓存|入口预留和出口确认，多个任务排队但平台容量1|20托盘，出口暂堵时入口缓存受控，恢复全部送达|缓存未到位就授权平台升降
3|two_rack_storage|双货架堆垛机存取|W02|pfeed1>load1>crane1>rack1,crane1>rack2,crane1>load2>exit1|适合叉取的托盘|增加第二货架，位置编号按导出配置映射，禁止自造坐标|两货架空位预约，叉伸缩与左右方向互锁|跨两架各6托盘入出库，记录12唯一货位映射|两货架位置号当作同一命名空间
3|storage_dual_infeed|双入口预约共享仓库|W03|pfeed1>roller1>merge1>load1>crane1>rack1,pfeed2>roller2>merge1,crane1>load2>exit1|托盘|两入口重载汇流，共用堆垛机装货口|先空位预约再允许入装货口，入口公平仲裁|两源各8托盘入库并出库，仓满时两源均禁止继续送入|货位已满但装货口仍收托盘
3|storage_dual_outbound|仓库双目的地出库|W03|pfeed1>load1>crane1>rack1,crane1>load2>turn1>roller1>exit1,turn1>roller2>exit2|托盘与外部出库目的地任务|装卸口后增加转台双出口，目的地不由货位号推断|出库订单绑定货位、物料、目标出口，卸口背压堆垛机|12托盘指定出库，两个出口各6且次序可核对|出库订单在转台前被覆盖
3|storage_rfid_tracking|RFID入库与位置追踪|W02|pfeed1>rfid1>load1>crane1>rack1,crane1>load2>rfid2>exit1|支持RFID的托盘|装货前和卸货后各一读写区，堆垛机独立货位表|RFID身份对应物料令牌，出库再次核对；重载不依赖会重置的视觉ID|10托盘身份、位置、出库三表一致，错读时停靠|重新加载后沿用旧视觉编号作为永久ID
4|palletize_store_ship|码垛入库与订单出库一体线|W03|pfbox1>palletizer1>roller1>load1>crane1>rack1,pfeed1>palletizer1,crane1>load2>rfid1>turn1>roller2>exit1,turn1>roller3>exit2|Palletizing Box和托盘|满垛输送到仓库，再经读写与双出口发运，货物兼容待验|满垛锁单、货位预约、叉取确认、出库派送；核对垛货叉取稳定性|4满托盘完成入库和指定出库，全程身份及箱数不变|只证明空托盘可叉取就假定满垛也通过
4|dual_crane_dispatch|双堆垛机与公共发运汇流|W03|pfeed1>load1>crane1>rack1,crane1>load3>zone1>merge1>exit1,pfeed2>load2>crane2>rack2,crane2>load4>zone2>merge1|适合叉取的托盘|两独立货架和堆垛机，出库各一重载缓存再公共合流|库存分库、订单选库、出口预约；单库停用保持另一库服务|两库各8托盘存取，公共出库16项可核对，停库不改写库存|复位把两个库的占用表一并清空
""",
"process_control": """
1|tank_timed_fill|单罐定时充排液|F01|timertank1|原生液体模型|一个罐体及原生进出阀，保留可观察液位仪表|定时充液、停阀、定时排液；先用仪表校验时长不越界|3周期液位不溢出且不排空后继续长时间开阀，时长由实测标定|定时常数照搬不适配初始液位
1|tank_switch_control|单罐上下液位开关控制|F01|dtank1|原生液体模型|数字阀加上下两个电容液位检测，安装可行性先核对|低位开始充、高位停止，排液阶段独立，信号去抖|3周期均在上下检测带间切换；两信号矛盾时停供液|高低液位传感器高度装反
2|tank_pid_level|模拟量液位闭环罐|F01|tank1|原生液体模型|原生液位模拟量和进出阀，液位换算按量程确认|PI或PID、输出限幅、抗积分饱和、手自动无扰切换|3次液位阶跃后误差进入拟定满量程百分之5带，记录超调和稳定时间|把0到10V直接当百分比液位
2|tank_flow_loop|流量闭环与液位监护|F02|tank1>flowwatch1|原生液体模型|单罐原生流量读数闭环，加独立高低液位开关；flowwatch为附加仪表模块|流量环控制出阀，高低液位优先覆盖流量命令|3流量设定点达到拟定百分之5量程误差；低液位时出阀关闭|流量环持续排液绕过低液位互锁
3|two_tank_supply|双罐共享逻辑供液配额|F03|tank1,tank2|两个独立原生液体模型|两个罐体均有独立阀和液位；共享供液是PLC逻辑配额，无物理联管|供液容量限制同时充液命令，轮询服务低液位请求|双罐各3补液周期，配额始终满足且都得到服务；不声称罐间物料守恒|把独立罐液位变化当作真实管路流量
3|dual_tank_batch|双罐交替批次充排|F03|dtank1,dtank2|两个独立原生液体模型|双罐数字阀、上下检测，批次记录独立|充液请求仲裁，一罐排液时另一罐准备；批次完成需液位实证|两罐各4批，批次编号和上下位事件匹配，异常批不计完成|相邻罐完成边沿误增另一罐批次
4|triple_tank_scheduler|三罐共享供液与隔离恢复|F03|tank1,tank2,tank3|三个独立原生液体模型|三罐独立液位阀门，共享供液调度抽象，隔离只覆盖逻辑设备状态|限额供液、请求优先级和饥饿上限；单罐传感器异常隔离|三罐各3周期加一次单罐故障，其余两罐完成计划，故障罐不判通过|坏液位信号继续参与供液优先级
4|dual_tank_supervised|双流量环罐体的监督控制|F03|tank1>flowwatch1,tank2>flowwatch2|两个独立原生液体模型|两独立流量闭环各配高低检测；批次协调是逻辑，不模拟混合或化学反应|底层闭环独立运行，监督层分配流量目标并处理通信暂停与手自动切换|两罐各3设定点及2次模式切换，阀限幅有效，无长时间饱和失控|上层暂停时把底层PID历史量无条件清零
""",
}

SOURCES = {
    "fio_scenes": {"title": "Factory I/O 官方场景目录", "url": "https://docs.factoryio.com/manual/scenes/", "kind": "capability_reference", "evidence": "已读官方目录；具体本机工程逐个核对"},
    "fio_create": {"title": "Factory I/O 创建场景", "url": "https://docs.factoryio.com/getting-started/creating-a-scene/", "kind": "capability_reference", "evidence": "已读官方文档"},
    "fio_light": {"title": "Factory I/O Light Load Parts", "url": "https://docs.factoryio.com/manual/parts/light-load/", "kind": "capability_reference", "evidence": "已读官方部件和模式说明"},
    "fio_heavy": {"title": "Factory I/O Heavy Load Parts", "url": "https://docs.factoryio.com/manual/parts/heavy-load/", "kind": "capability_reference", "evidence": "已读官方部件和模式说明"},
    "fio_stations": {"title": "Factory I/O Stations", "url": "https://docs.factoryio.com/manual/parts/stations/", "kind": "capability_reference", "evidence": "已读官方站点说明；具体选型待本机确认"},
    "fio_sensors": {"title": "Factory I/O Sensors", "url": "https://docs.factoryio.com/manual/parts/sensors/", "kind": "capability_reference", "evidence": "已读官方检测类型说明"},
    "fio_items": {"title": "Factory I/O Items", "url": "https://docs.factoryio.com/manual/parts/items/", "kind": "capability_reference", "evidence": "已读官方物料说明"},
    "festo_mps": {"title": "Festo MPS 工艺单元资料（2007）", "url": "https://www.festo.com/net/supportportal/files/10142/mps.pdf", "kind": "process_reference", "evidence": "已读官方历史资料；只参考工艺，不作为现售型号清单"},
    "siemens_sort": {"title": "Siemens SCE 分拣站工艺描述（2019）", "url": "https://www.automation.siemens.com/sce-static/learning-training-documents/tia-portal/example-processes/sce-020-100-process-description-sorting-station-r1904-de.pdf", "kind": "process_reference", "evidence": "已读官方德文工艺资料"},
    "dematic_gap": {"title": "Dematic Patagonia 物流案例", "url": "https://www.dematic.com/en-us/insights/case-studies/how-big-brands-deliver-roi-with-green-initiatives/", "kind": "process_reference", "evidence": "已读厂商案例页；只提取拉距和测量需求"},
    "dematic_sort": {"title": "Dematic 模块化分拣系统", "url": "https://www.dematic.com/content/dam/dematic/downloads/brochures/NA_BR_1112_ModularSortSystemSolution.pdf", "kind": "process_reference", "evidence": "官方搜索索引确认；完整附件精读待补"},
    "dematic_zone": {"title": "Dematic 拣选与配套输送", "url": "https://www.dematic.com/en-us/insights/articles/conveyor-systems-for-order-picking-and-kitting/", "kind": "process_reference", "evidence": "官方搜索索引确认；逐段流程精读待补"},
    "flexlink_pallet": {"title": "FlexLink 托盘输送系统", "url": "https://www.flexlink.com/en/portfolio/conveyor-systems/pallet-conveyors", "kind": "process_reference", "evidence": "官方搜索索引确认；直接抓取失败，材料待补"},
    "flexlink_endline": {"title": "FlexLink 十一产线末端码垛案例", "url": "https://www.flexlink.com/en/about-us/success-stories/integrated-end-of-line-automation-streamlines-palletizing-across-11-food-production-lines", "kind": "process_reference", "evidence": "官方搜索索引确认；直接抓取失败，材料待补"},
    "vc_pallet": {"title": "Visual Components 码垛机器人课程", "url": "https://academy.visualcomponents.com/lessons/palletizing-robot-programming/", "kind": "process_reference", "evidence": "已读课程页，未观看完整视频"},
    "bili_course": {"title": "工控圈 Factory I/O 联合仿真教程", "url": "https://www.bilibili.com/video/BV12B9EYMEKt/", "kind": "tutorial_reference", "evidence": "章节搜索索引确认；访问412，未观看完整视频或下载附件"},
    "inst_course": {"title": "Instrumentation Tools Factory I/O 课程", "url": "https://instrumentationtools.com/factory-io-plc-automation/", "kind": "tutorial_reference", "evidence": "已读作者课程目录，未逐视频验收"},
    "acc_pallet": {"title": "ACC Automation 码垛控制教程", "url": "https://accautomation.ca/plc-programming-example-palletizer-drum-instructions/", "kind": "tutorial_reference", "evidence": "已读作者教程页；附件内容待检查"},
    "ahmed_line": {"title": "Ahmedhh1218 生产线工程", "url": "https://github.com/Ahmedhh1218/TIA-FACTORY-IO-Production-Line-Automation", "kind": "candidate_project", "evidence": "文件树确认1个场景；未下载运行；许可元数据MIT仍需逐文件核对"},
    "far2_smart": {"title": "Far2Deluxe Smart Factory 工程", "url": "https://github.com/Far2Deluxe/Smart-Factory-Simulation-FactoryIO-PLC-", "kind": "candidate_project", "evidence": "文件树确认1个场景；未下载运行；许可元数据缺失"},
    "faaizz": {"title": "Faaizz 模块化工厂工程", "url": "https://github.com/Faaizz/tuk_project", "kind": "candidate_project", "evidence": "文件树确认18场景路径；含Old；未去重运行；许可元数据缺失"},
}

FAMILY_SOURCES = {
    "transport": ["fio_create", "fio_light", "fio_heavy", "dematic_gap"],
    "routing": ["fio_heavy", "fio_light", "flexlink_pallet", "bili_course"],
    "buffer_merge": ["fio_light", "fio_heavy", "festo_mps", "dematic_sort"],
    "sorting": ["fio_sensors", "fio_light", "siemens_sort", "dematic_sort"],
    "handling": ["fio_stations", "festo_mps", "dematic_zone"],
    "machining": ["fio_stations", "fio_items", "festo_mps", "ahmed_line"],
    "assembly": ["fio_stations", "fio_items", "festo_mps", "faaizz"],
    "palletizing": ["fio_stations", "fio_items", "vc_pallet", "flexlink_endline", "acc_pallet"],
    "warehouse": ["fio_stations", "fio_heavy", "far2_smart", "bili_course"],
    "process_control": ["fio_stations", "fio_sensors", "inst_course"],
}

# B designs preserve workflow with an explicit abstraction or require a compatibility experiment.
# A is a proposed native mapping, not a capability or acceptance pass.
ABSTRACTIONS = {
    "robot_order_kitting": "B：只确认两个料位的配对交付，不模拟人工装袋、拧紧或包装。",
    "machine_order_warehouse": "B：本期仅实现装箱及入库接口；仓库作为后续独立系统对接。",
    "assembly_quality_reject": "B：成品外观类型的自动检测能力未确认，必须允许人工图像标注，不能声称原生视觉已支持。",
    "assembly_carrier_dispatch": "B：Final Product 在承载箱内的稳定承载待物理试验；不支持时重新设计载具。",
    "pallet_pool_three_lines": "B：只实现供箱、码垛和托盘流转；不实现真实封膜、包装或食品生产过程。",
    "palletize_store_ship": "B：满垛托盘的叉取和仓储稳定性待单独验证，不能用空托盘结果替代。",
    "two_tank_supply": "B：两个罐是独立原生对象；共享供液仅为逻辑资源配额，不存在经验证的相连管路。",
    "dual_tank_batch": "B：两罐协调批次是逻辑任务，不表示真实跨罐输送、混合或化学过程。",
    "triple_tank_scheduler": "B：三罐协调采用逻辑供液配额；隔离为控制层状态，不表示物理管路切断。",
    "dual_tank_supervised": "B：两罐独立闭环，上层协调只分配任务；不模拟混合、温度或化学反应。",
}

PILOT_SLUGS = [
    "single_conveyor", "two_section_handoff", "height_two_way", "pallet_turntable",
    "two_to_one_merge", "weight_two_way", "color_three_way", "two_axis_pick",
    "single_base_machine", "base_lid_assembly", "builtin_palletizer", "two_floor_elevator",
    "one_rack_storage", "tank_switch_control", "tank_pid_level", "xyz_pallet_single",
    "machine_to_assembly", "xyz_pallet_two_levels", "storage_rfid_tracking", "dual_source_multisort",
]
