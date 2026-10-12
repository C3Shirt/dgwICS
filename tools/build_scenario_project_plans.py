r"""Materialize the curated 100-project design library, or validate it without changing files.

Usage: .venv\Scripts\python.exe tools/build_scenario_project_plans.py
       .venv\Scripts\python.exe tools/build_scenario_project_plans.py --validate
Creation refuses an existing output directory, preserving subsequent manual work.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from scenario_plan_data import ABSTRACTIONS, FAMILIES, FAMILY_SOURCES, PILOT_SLUGS, ROWS, SOURCES
from scenario_plan_modules import GROUP_NAMES, MODULES

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "scenario_projects"
DATE = "2026-10-10"
LEVEL_NAMES = {1: "基础", 2: "单元协同", 3: "多单元生产", 4: "综合与恢复"}
LEVEL_QUOTAS = {1: 20, 2: 30, 3: 30, 4: 20}
GATES = ["design_review", "capability_probe", "scene_load", "io_binding", "plc_compile",
         "plc_download_run", "protocol_connection", "deterministic_acceptance", "visual_acceptance", "training_review"]
MACHINE_LIDS = {
    "single_lid_machine_buffer": {"machine1"},
    "lid_base_split_machine": {"machine1"},
    "robot_twin_machining": {"machine2"},
    "machine_to_assembly": {"machine2"},
    "full_manufacture_assemble": {"machine2", "machine4"},
}


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def node_kind(node_id: str) -> str:
    match = re.fullmatch(r"([a-z]+)([1-9][0-9]*)", node_id)
    if not match or match[1] not in MODULES:
        raise ValueError(f"Unknown module: {node_id}")
    return match[1]


def parse_graph(expression: str) -> tuple[list[dict], list[dict]]:
    ids, pairs = [], []
    for route in expression.split(","):
        chain = route.split(">")
        for node_id in chain:
            node_kind(node_id)
            if node_id not in ids:
                ids.append(node_id)
        for source, target in zip(chain, chain[1:]):
            if (source, target) not in pairs:
                pairs.append((source, target))
    # Rack edges are bidirectional material operations: storage and retrieval are separate events.
    edges = []
    for source, target in pairs:
        kind = "observation" if node_kind(target) == "flowwatch" else "material_transfer"
        if node_kind(target) == "rack":
            kind = "storage"
            edges.append({"source": target, "target": source, "kind": "retrieval"})
        edges.append({"source": source, "target": target, "kind": kind})
    nodes = [{"id": n, "kind": node_kind(n)} for n in ids]
    return nodes, edges


def io_roles(nodes: list[dict]) -> list[dict]:
    result = []
    for node in nodes:
        for declaration in MODULES[node["kind"]][3].split():
            direction, data_type, role = declaration.split(":")
            result.append({
                "symbol": f"{direction}_{node['id']}_{role}", "node": node["id"],
                "controller_direction": "output" if direction == "Q" else "input",
                "planned_type": data_type, "role": role,
                "origin": "proposed_role_needs_tag_or_sensor_confirmation",
                "binding_status": "unresolved", "object_guid": None, "io_guid": None,
                "point_io_key": None, "driver_channel": None, "tag_address": None,
                "polarity": None, "scale": None,
            })
    return result


def cases(p: dict) -> list[dict]:
    fluid = p["family"] == "process_control"
    common_oracle = (
        "从独立仪表/截图核对液位边界、阀限幅和周期；不能仅使用控制器的Done位。" if fluid else
        "测试投料/订单表由验收者先冻结，结合出口检测、轨迹和图像逐项核对；期望值不能由被测PLC生成。"
    )
    result = [
        {"case_id": "C01", "name": "本机部件能力与空载检查", "setup": "不供料；逐一检查模块选型、完整Tags和工作模式。",
         "expected": "有明确物理实现与完整I/O；未确认部件或反馈时返回needs_attention。"},
        {"case_id": "C02", "name": "最小工艺周期", "setup": p["material_contract"]["bootstrap_unit"] + "；关闭任务并发，复合工艺允许一个任务所需的多个组成件进入。",
         "expected": "过程各步骤有独立确认，不跳过取放、检测、接货或液位事件。"},
        {"case_id": "C03", "name": "项目功能验收", "setup": p["materials"] + "；按预先冻结测试表执行。",
         "expected": p["acceptance"] + "。" + common_oracle},
        {"case_id": "C04", "name": "项目修复任务", "setup": p["repair"] + "；仅在副本中受控注入一次。",
         "expected": "定位到具体对象/位置/模式/逻辑问题；修复后重跑C02、C03，保存修改前后证据，不给故障运行打成功标签。"},
        {"case_id": "C05", "name": "停止与恢复", "setup": "在有物料或有液位状态时请求停止，再在核对实物后恢复。",
         "expected": "保持物料/库存/夹持/周期的真实状态，不清空现场后假装连续恢复；需人工处理则标needs_attention。"},
        {"case_id": "C06", "name": "Agent3多视角复核", "setup": "拍总览、关键接触面和关键步骤；配同步Tag轨迹。",
         "expected": "没有落地、穿插、悬空、越界或账实不符；遮挡或证据不足返回inconclusive。"},
    ]
    if p["level"] >= 2:
        result.append({"case_id": "C07", "name": "资源占用与进阶负载", "setup": p["concurrency"]["target_contract"],
                       "expected": "每个物理区容量和资源锁均不超限；所有源得到服务，逐件/逐批关联不中断，不能只通过单件测试就宣告此项通过。"})
    if p["level"] == 4:
        result.append({"case_id": "C08", "name": "综合中断与重启审计", "setup": "断开仿真通信或停用一个资源；恢复后先读回现场状态与版本。",
                       "expected": "失联状态明确，不重用陈旧指令；不能无证据宣称软件重载后物料ID、库存或设备位置保持。"})
    for case in result:
        case.update({"status": "planned", "observed_result": None, "evidence_paths": []})
    return result


def build_records() -> list[dict]:
    records = []
    for family, text in ROWS.items():
        for line in text.strip().splitlines():
            columns = line.split("|")
            if len(columns) != 10:
                raise ValueError(line)
            level, slug, title, group, flow, material, layout, control, acceptance, repair = columns
            nodes, edges = parse_graph(flow)
            sources = list(FAMILY_SOURCES[family])
            for node in nodes:
                kind = node["kind"]
                node.update({"name": MODULES[kind][0], "composition_draft": MODULES[kind][1],
                             "mode_draft": MODULES[kind][2], "constraint": MODULES[kind][4],
                             "prefab_name": None, "object_guid": None, "position": None,
                             "configuration_status": "planned"})
                if kind == "machine":
                    node["produce_lids_draft"] = node["id"] in MACHINE_LIDS.get(slug, set())
                if kind in {"vision", "metal", "array", "rfid", "height"} and "fio_sensors" not in sources:
                    sources.append("fio_sensors")
                if kind in {"xyz", "pick", "lift", "crane", "palletizer", "assemble", "tank", "dtank", "timertank"} and "fio_stations" not in sources:
                    sources.append("fio_stations")
            sources.append("fio_items") if "fio_items" not in sources else None
            fidelity = ABSTRACTIONS.get(slug, "A：拟采用原生部件实现这一工艺；部件模式、兼容性及运行表现仍需本机验收。")
            is_fluid = family == "process_control"
            kinds = {n["kind"] for n in nodes}
            compound_job = bool(kinds & {"assemble", "palletizer", "pdock", "boxdock"}) or slug == "robot_order_kitting"
            recipe = "单物料从供料到出库/存入货位的完整任务"
            if is_fluid:
                recipe = "单罐一个充排周期或一次设定值响应"
            elif slug == "robot_order_kitting":
                recipe = "一套配对交付任务：源1一件原料＋源2一件原料，交付后逻辑确认"
            elif "assemble" in kinds:
                recipe = "一套装配任务：1 Base＋1 Lid→1实际Final Product"
                if "boxdock" in kinds:
                    recipe += "，再加1承载箱完成装箱"
            elif "palletizer" in kinds or "pdock" in kinds:
                recipe = "一个满托盘任务：N Palletizing Box＋1 Pallet→1满托盘；N由已标定垛型确定"
            elif "boxdock" in kinds:
                recipe = "一个装箱任务：1 Raw Material加工为Base＋1 Stackable Box→1载件箱"
            material_contract = {
                "bootstrap_unit": recipe, "compound_job": compound_job,
                "component_identity_required": compound_job, "parameters": "垛型箱数、颜色配套、载箱容量均在能力验证后冻结",
                "trace_accounting": "复合任务保留父任务和每个组成件身份，区分运输在途、等待库存、在制品和已完成产物；转换和聚合不按单件ID一进一出强行核算",
            }
            concurrency = {
                "bootstrap": "single_cycle" if is_fluid else "single_job" if compound_job else "single_item",
                "bootstrap_max_in_flight": None if is_fluid or compound_job else 1,
                "bootstrap_max_jobs": 1,
                "bootstrap_component_limit": "按一个工艺任务的BOM及各实际区容量控制；不可设置全场仅允许一个组成件" if compound_job else "按物理资源容量标定",
                "target_contract": (
                    "多罐按各自状态运行，共享供液受逻辑配额约束；先测单罐再验证同时请求。" if is_fluid and len([n for n in nodes if 'tank' in n['kind']]) > 1 else
                    "单罐按拟定周期/闭环运行，验证设定变化与互锁优先级。" if is_fluid else
                    "先完成一个完整工艺任务，允许其所需组成件逐步供入；后续增加并行任务时，按工位、载具和组成件分别限制容量并关联身份。" if compound_job else
                    "单件闭环，Emitter初验UpTo=1，上一件出库后才供下一件。" if int(level) == 1 else
                    f"单件通过后递增到拟定{2 if int(level)==2 else 4 if int(level)==3 else 6}件在途；实际总容量先测定，每物理停靠位/秤台/夹爪/升降平台默认容量1，不能超过校准容量。"
                ),
                "target_capacity_verified": False,
            }
            p = {
                "schema_version": 1, "design_version": 1, "created_date": DATE,
                "family": family, "family_name": FAMILIES[family][0], "level": int(level), "level_name": LEVEL_NAMES[int(level)],
                "slug": slug, "title": title, "template_group": group, "template_name": GROUP_NAMES[group],
                "status": "planned", "scope": "design_package_only", "fidelity": fidelity[0], "fidelity_note": fidelity,
                "requirement": f"构建{title}场景，以{material}为工艺对象，完成以下可观察控制目标：{control}。",
                "materials": material, "structural_difference": layout, "layout_constraints": layout,
                "process_graph": {"notation": "module-level proposed flow; not calibrated prefab ports", "expression": flow, "nodes": nodes, "edges": edges},
                "controller_plan": control, "acceptance": acceptance, "repair": repair,
                "io_roles": io_roles(nodes), "io_contract_status": "semantic_draft_incomplete",
                "required_bindings_complete": False, "concurrency": concurrency,
                "material_contract": material_contract,
                "sources": sources, "provenance": {"design_origin": "project_authored_proposal", "copied_external_project": False,
                    "external_references_role": "capability and process inspiration; not evidence for this exact combined layout",
                    "attachments_downloaded": False, "reuse_rights_review": "pending_before_external_content_enters_training"},
                "gates": {g: "pending" for g in GATES}, "training": {"sft_eligible": False, "human_reviewed_design": False,
                    "verified_positive_samples": 0, "verified_repair_samples": 0, "split": "unassigned", "split_group": group},
                "artifact_paths": {"reference_materials": "materials/references", "scene": "materials/scene",
                    "plc": "materials/plc", "evidence": "materials/evidence", "training": "materials/training"},
            }
            records.append(p)
    records.sort(key=lambda p: (p["level"], list(FAMILIES).index(p["family"])))
    for i, p in enumerate(records, 1):
        p["id"] = f"SC{i:03d}"
        p["directory"] = f"{p['id']}_{p['slug']}"
        prior = [q for q in records if q["level"] < p["level"]]
        dependencies = []
        prerequisite_families = [p["family"]] if p["family"] == "process_control" else ["transport", p["family"]]
        for family in dict.fromkeys(prerequisite_families):
            candidates = [q for q in prior if q["family"] == family]
            if candidates:
                pilot_candidates = [q for q in candidates if q["slug"] in PILOT_SLUGS]
                # A new family capability may first be probed inside its pilot's C01.
                # Do not silently require an unselected project for the closed 20-pilot curriculum.
                if p["slug"] in PILOT_SLUGS and not pilot_candidates:
                    continue
                best = max(pilot_candidates or candidates, key=lambda q: (q["level"], PILOT_SLUGS.index(q["slug"]) if q["slug"] in PILOT_SLUGS else 0))
                dependencies.append(best["id"])
        if p["slug"] in {"machine_to_assembly", "full_manufacture_assemble"}:
            dependencies.extend(q["id"] for q in prior if q["slug"] == "single_base_machine")
        p["dependencies"] = list(dict.fromkeys(dependencies))
        p["acceptance_cases"] = cases(p)
    validate_inputs(records)
    return records


def signature(p: dict) -> str:
    graph = p["process_graph"]
    nodes = Counter((n["kind"], n.get("produce_lids_draft")) for n in graph["nodes"])
    edges = Counter((node_kind(e["source"]), node_kind(e["target"]), e["kind"]) for e in graph["edges"])
    return repr((sorted(nodes.items(), key=str), sorted(edges.items(), key=str)))


def validate_inputs(records: list[dict]) -> None:
    assert len(records) == 100
    assert Counter(p["family"] for p in records) == {f: q for f, (_, q) in FAMILIES.items()}
    assert Counter(p["level"] for p in records) == LEVEL_QUOTAS
    assert len({p["slug"] for p in records}) == 100
    assert len({p["id"] for p in records}) == 100
    assert len({p["template_group"] for p in records}) == 36
    collisions = {}
    for p in records:
        s = signature(p)
        if s in collisions:
            raise ValueError(f"Structural draft collision: {p['slug']} vs {collisions[s]}")
        collisions[s] = p["slug"]
        assert set(p["sources"]) <= set(SOURCES)
        assert len(p["io_roles"]) == len({r["symbol"] for r in p["io_roles"]})
        if p["material_contract"]["compound_job"]:
            assert p["concurrency"]["bootstrap"] == "single_job"
            assert p["concurrency"]["bootstrap_max_in_flight"] is None
            assert p["concurrency"]["bootstrap_max_jobs"] == 1
        ids = {n["id"] for n in p["process_graph"]["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in p["process_graph"]["edges"])


def bullets(values: list[str]) -> str:
    return "\n".join("- " + x for x in values)


def project_markdown(p: dict, by_id: dict) -> str:
    nodes = p["process_graph"]["nodes"]
    graph = [f'    {n["id"]}["{n["id"]} {n["name"]}"]' for n in nodes]
    for e in p["process_graph"]["edges"]:
        arrow = "-.->|观察/互锁|" if e["kind"] == "observation" else "-->|取货|" if e["kind"] == "retrieval" else "-->|存货|" if e["kind"] == "storage" else "-->"
        graph.append(f"    {e['source']} {arrow} {e['target']}")
    bom = []
    for n in nodes:
        mode = n["mode_draft"]
        if "produce_lids_draft" in n:
            mode += f"；ProduceLids={'TRUE（盖）' if n['produce_lids_draft'] else 'FALSE（底）'}"
        bom.append(f"| {n['id']} / {n['name']} | {n['composition_draft']} | {mode} |")
    interfaces = [f"| {r['symbol']} | {r['controller_direction']} | {r['planned_type']} | {r['role']} | 待绑定 |" for r in p["io_roles"]]
    constraints = [f"**{n['id']}**：{n['constraint']}。" for n in nodes]
    dependencies = ", ".join(f"[{pid} {by_id[pid]['title']}](../{by_id[pid]['directory']}/design.md)" for pid in p["dependencies"]) or "无项目依赖；先完成软件版本、加载、截图与Tag导出的公共能力检查。"
    acceptance = [f"| {c['case_id']} {c['name']} | {c['setup']} | {c['expected']} | 待执行 |" for c in p["acceptance_cases"]]
    fluid = p["family"] == "process_control"
    inventory = "液位/流量/阀位/批次分别记录；跨罐没有物理连接时，不建立虚假的跨罐质量守恒。" if fluid else "保留投料、在途、出库和隔离账；装配按1底+1盖→1成品，加工记录原料→产品，码垛按箱和托盘两层核算。"
    return f"""# {p['id']} {p['title']}

[返回总目录](../README.md) · [共享规则](../shared-design-rules.md) · [机器可读方案](project.json) · [来源](sources.md) · [IO草案](io-plan.csv)

设计版本：1；日期：{DATE}；状态：**planned / 设计方案**。工艺族：{p['family_name']}；难度：L{p['level']} {p['level_name']}；模板组：{p['template_group']} {p['template_name']}。

本目录尚无已验收的 `.factoryio`、CODESYS `.project`、ST 或运行证据。以下坐标、容量、标签和验收数值中需要标定的内容仍是设计参数。依赖完成不等于本项目完成。

## 1. 需求与实现边界

{p['requirement']}

- 物料：{p['materials']}。
- 有效结构差异：{p['structural_difference']}。
- 最小工艺任务：{p['material_contract']['bootstrap_unit']}。
- 身份与物料核算：{p['material_contract']['trace_accounting']}。
- 仿真保真级别：{p['fidelity_note']}
- 本方案由项目自主设计；外部来源用于工艺启发或部件能力核对，并未提供或验证本方案的全部组合。

## 2. 工艺拓扑

节点是工艺模块，传感器及辅助输送在模块清单中展开；图不是已经校准的机械端口连接图。货架的存取为两个方向事件，罐体监护的虚线不表示管道。

```mermaid
flowchart LR
{chr(10).join(graph)}
```

## 3. 对象、工作模式与布局

| 模块 | 拟选对象 / 最小组成 | 配置草案 |
| --- | --- | --- |
{chr(10).join(bom)}

此表是模块级BOM草案。入口/出口数变化时必须增配检测、导入输送和定位对象，不能把节点数当成最终Object数。面板、Start/Stop/Reset、状态灯等公共对象参见共享规则。

布局重点：**{p['layout_constraints']}**。

先规划水平占地和运动包络，再匹配各接货面的高度，最后调传感器光路、夹持/放料点与相机。`.factoryio`世界坐标为X/Z水平、Y竖直；工作站局部轴和工程师习惯的“Z为高度”要显式转换。数值单位与原点先标定，本方案不编造精确坐标。

{bullets(constraints)}

## 4. IO语义契约草案

方向从PLC看：output驱动物体，input读取现场。下表仅提供**语义角色和拟定类型，尚未覆盖完整原生接口**；特别是抓放轴到位、码垛握手、堆垛机叉取反馈必须导出Tags后补齐。来源、数据类型或真实反馈不支持时需修改设计，不能创建同名虚拟量冒充物理输入。

| 符号 | PLC方向 | 拟定类型 | 语义角色 | 绑定 |
| --- | --- | --- | --- | --- |
{chr(10).join(interfaces) or '| 无原生信号 | — | — | 仅几何对象 | 待确认 |'}

另有PLC内部或任务接口：`L_State`、`L_Request`、`L_Target`、`L_ResourceOwner`、`L_ItemOrBatchToken`、`L_Timeout`、`L_StableDerived`、`L_CompletedCount`。这些变量不自动成为Factory I/O Tags。操作按钮和协议状态属于共享接口，完整合同阶段再展开。

所有GUID、PointIOKey、驱动通道、Tag Address、极性和换算系数当前为空。实例化后导出真实Tags，再做一对一绑定与读回验证；BOOL、INT、DWORD和REAL的Modbus表示、字序与寄存器跨度分别校验。

## 5. PLC方案

核心逻辑：**{p['controller_plan']}**。

生命周期：`IDLE → REQUEST/RESERVE → EXECUTE → VERIFY → RELEASE → COMPLETE`，失败转`FAULT/HOLD`；这是工艺骨架，不是所有设备共用一个全局步号。每个并行单元应有自己的状态机，共享资源由独立仲裁块管理。

- 函数块建议：模块控制块、`FB_Handshake`、`FB_ResourceArbiter`、`FB_ItemOrBatchTracker`、超时/边沿块；模拟罐另有独立闭环控制块。
- 到位优先采用真实反馈。定时器只做过程条件或超时保护，不能证明夹爪到位、转台到角、货物出库等事实。
- 停止、复位和重新供料分开；失联后先暂停派发和读回状态。不得在未核对现场前清库存、队列或夹持记录。
- {inventory}

首轮策略：{p['concurrency']['bootstrap']}。目标策略：{p['concurrency']['target_contract']}

## 6. 验收与修复数据

验收用例见[acceptance-plan.json](acceptance-plan.json)。数量、容差和时间目前是试验目标；需先用真实对象验证可行性，再冻结测试合同。表中“待执行”不代表通过。

| 用例 | 准备 / 操作 | 独立期望与判定 | 状态 |
| --- | --- | --- | --- |
{chr(10).join(acceptance)}

强制区分：加载成功、编译成功、PLC Run、协议连接、Factory I/O运行、逐项工艺验收、Agent3图像复核。只有工艺与视觉两门都通过，才能产生经审核的成功轨迹；证据不足保持inconclusive。

## 7. Agent交互与SFT材料计划

1. Agent1提交需求分解、拓扑、对象选择、物料转换、验收合同与保真边界。
2. Agent2提交生成代码、场景文件、对象/模式/真实IO清单及布局差异；确定性工具执行静态检查与加载。
3. Agent3消费同步多视图、轨迹和对象身份，反馈对象、问题类别、观测证据、建议约束、置信度；修改布局交Agent2，修改逻辑交PLC Agent。
4. PLC Agent以冻结IO合同生成ST/GVL与状态机；工具层编译、下载、运行、读回协议并执行验收，不能以模型自报成功代替观察。
5. 本项目修复练习：{p['repair']}。保留故障副本、原因、修改diff与复验，成功与失败标签分别记录。

所有交接携带task/scene/io/plc/config/policy版本、输入SHA、预期产物和修复历史。每次改几何或模式都使相关IO绑定和验收版本失效。过期反馈不得继续执行。

当前SFT状态：**不具备训练资格**。需求/规划稿先人工复核；代码和正向/修复轨迹需要真实产物与证据。归档可观察理由，不生成虚假的隐藏思维过程。谱系组`{p['template_group']}`整体划分训练/验证/测试，不在同组内随机拆场景。

## 8. 依赖、待确认项与材料位置

建议实现前置：{dependencies}

- 核对本机软件版本、全部所需Prefab、模式和完整原生反馈接口。
- 标定端口接触面、轴行程、世界/局部坐标、量程、传感器极性与停车距离。
- 测量容量和超时；并发、故障和恢复不能沿用未经验证的单件假设。
- 核对外部附件可用性、谱系、许可与兼容性；有来源链接不代表附件已收集。
- 在[materials/README.md](materials/README.md)列出的五类目录中补材料；已有研究工程保留原位，仅在明确选定种子后复制。
"""


def source_markdown(p: dict) -> str:
    rows = []
    for sid in p["sources"]:
        source = SOURCES[sid]
        rows.append(f"| {sid} | [{source['title']}]({source['url']}) | {source['kind']} | {source['evidence']} |")
    local_seed = (
        "\n本项目另有本仓库重量分拣调试经验可作候选种子：[调试流程记录](../../docs/agent-workflow-lessons.md)。本轮没有复制已有场景或PLC，也不把历史运行结果当成本目录的新验收。\n"
        if p["slug"] == "weight_two_way" else ""
    )
    return f"""# {p['id']} 来源与工艺映射

设计主体为自主提出的`{p['title']}`。这些链接是该工艺族的能力/工艺参考，**不是每个来源都描述了本方案，也不是100个外部工程已经下载**。制造商案例迁移时只保留可观察流程和接口，新增组合为本项目设计。

| 来源ID | 材料 | 作用 | 证据与限制 |
| --- | --- | --- | --- |
{chr(10).join(rows)}
{local_seed}
映射：原始工业需求→{p['materials']}→{p['structural_difference']}→{p['fidelity_note']}

需人工补充：参考文件/视频章节、观察到的原始流程、图与时间戳、作者许可、复用范围、与本项目的具体差异。现在`materials/references/`尚无下载附件。使用他人代码或视频作为训练数据前单独核对许可，引用网页和自主工艺设计分别记录。

完整来源注册表：[sources.json](../sources.json)。调查背景：[scene-dataset-plan.md](../../docs/scene-dataset-plan.md)。
"""


def materials_markdown(p: dict) -> str:
    return f"""# {p['id']} 材料清单

本期已完成目录和设计草案；以下实现/验收材料**尚待补充**。不要用空文件伪装已生成工程。

| 子目录 | 后续应放什么 | 当前 |
| --- | --- | --- |
| references/ | 工艺原文摘要、许可记录、视频章节与时间戳、参考布局；来源下载需登记 | 无附件，链接见../sources.md |
| scene/ | 生成代码、`.factoryio`、对象/模式清单、GUID映射、Tags CSV、布局标定 | 待实现 |
| plc/ | ST/GVL、CODESYS `.project`、IO映射、编译和下载报告；凭证只用环境配置 | 待实现 |
| evidence/ | 场景加载日志、协议读回、Tag时序、同步截图/视频、逐项验收、失败与修复diff | 待运行 |
| training/ | 按角色组织的样本、证据引用、人工审核、来源谱系与数据划分 | 训练资格未通过 |

索引保存在[project.json](../project.json)，验收合同在[acceptance-plan.json](../acceptance-plan.json)。所有实现产物记录版本和SHA；与物料状态、账号密码相关的内容不要写入训练文本。
"""


def shared_rules() -> str:
    return """# 场景项目共享设计与验收规则

## 范围与计数

本库是100份规划材料，不是100份可运行工程。36个模板组是拟定谱系；更严格的机械图去重和外部种子谱系核对仍待开展。相同拓扑只换名称、全局坐标、镜头、阈值或提示词不新增正常场景。故障副本也不新增正常场景数。

A为拟原生映射；B保留工艺但含明确抽象或兼容性待试验；二者都不是通过状态。C类如真实AGV导航、机器人六轴动力学、化学反应暂不进入本库的原生完成数量。

## 机械与对象

规划顺序：需求和物料转换→模块/对象及模式→水平拓扑与运动包络→接触面高度→传感器与轴行程→供料/移除→标签合同→PLC→运行验收。原生Tag、工作模式和材料可用性需要本机能力探测，不能用来源网页直接证明实例可以跑。

世界坐标X/Z水平、Y竖直。先标定坐标单位、对象原点、局部轴和接触面，再给坐标值。对象原点不等于输送面。移动工位留扫掠与安全抬升空间，边界传感器在Remover之前；截图相机要覆盖关键接触面，避免仅有总览。

模块BOM是起点；按实际入口/出口补检测、导入输送和支承。公共对象建议：Start/Stop/Reset按钮、运行/故障灯、操作模式开关、控制面板。急停交互只模拟工艺中断，不据此声称符合真实设备功能安全等级。

## IO合同与PLC接口

io-plan.csv是语义草案，不是可下载映射。符号方向从PLC看。完整原生标签导出后填写Object GUID、IO GUID、PointIOKey、模式、群组、通道、Tag Address、极性、类型、量程、强制及故障属性。驱动通道和Tag Address不得混用。保留forced、open/short circuit信息，正常验收排除非预期强制。

IO合同必须覆盖完整控制与反馈；当前抓放、码垛、堆垛模块尤其待补。没有原生Busy、Stable、AtTarget之类标签时，用经校验的真实反馈推导PLC内部量，并明确标记derived；不能冒充现场输入。数值型协议转换、DWORD/REAL跨度与字序须用受控写读回校验。

资源锁、预约、请求、物料/批次令牌、配方、派送订单、库存、完成数和闭环历史属于逻辑接口，不一定是Factory I/O Tags。永久身份不能沿用重新加载后会变化的视觉ID。RFID只对受支持的物料并按真实命令握手使用，任务字段预写有单独测试步骤。

一个共享资源一个仲裁器，一个并行单元一个状态机。单件运输先验证单物料闭环，装配/码垛/装箱先验证一个完整工艺任务，允许任务所需的多个组成件进入；不能把全场max_in_flight=1套在需要一盖一底或N箱一托盘的工艺上。复合任务保留父任务与组成件身份，区分运输在途、等待库存、在制品和产物。L2及以上再验证多件/多任务跟踪、区段容量、公平性和背压；首周期通过不替代进阶并发验收。罐控制按周期和仪表记录，不用箱体计数逻辑套用液体。

## 工艺与证据

加工记录Raw Material到Base/Lid；装配必须形成真实Final Product，不能从Palette直接生成成品充数。颜色检测只用于可支持的物料类别。内置码垛使用Palletizing Box。载箱和满垛叉取分别做兼容性试验，不能从空载推断带载稳定。

检测和验收独立：先冻结投料/订单/目标轨迹，再运行被测PLC。保存同步Tag轨迹、对象身份、多视图和实际物料去向。PLC计数只能作为证据之一。时长、量程、容差、阈值、并发容量均需标定后冻结，表中示例数量是设计测试目标。

状态门依次记录design_review、capability_probe、scene_load、io_binding、plc_compile、plc_download_run、protocol_connection、deterministic_acceptance、visual_acceptance、training_review。门不能跳级自报；不足证据返回inconclusive，设备/能力缺失返回needs_attention。

## 多Agent交接与修复

Agent1给出可验证设计与验收；Agent2维护几何、对象模式及场景产物；Agent3给带对象身份、截图位置和轨迹时刻的缺陷报告；PLC Agent维护控制与IO合同。确定性工具执行加载、编译、下载、运行、读回、采集及规则判定。模型不直接绕过控制层高频操纵现场。

交接携带task/scene/io/plc/config/policy版本、输入路径和SHA、预期产物与修复历史。改变输入后拒绝旧反馈；改几何/模式重新导出和验证IO。一次只修一个可检验问题，保留失败副本、根因、diff与复验；需要多个改动时说明因果依据。

## LoRA-SFT准备

先积累人工审核的规划稿；场景生成、PLC生成、视觉诊断、修复分别积累证据支持的输入输出。没有截图或运行轨迹时不制造Agent3判断。失败和inconclusive不能标正样本。当前所有100项目sft_eligible=false，未设置模型或训练作业。

36模板组只是初始split_group；导入外部工程后合并共同祖先和实际近似图，再整体划分训练/验证/测试。提示词改写、故障、相机、阈值变体跟随同一谱系。分阶段报告未知拓扑、未知组合、未知任务等泛化结果。许可未清楚的外部代码和素材不得自动进入训练集合。

## 项目材料结构

每项目：design.md、project.json、io-plan.csv、acceptance-plan.json、sources.md、materials/README.md，以及references/scene/plc/evidence/training五个待填目录。当前没有工程、程序或训练样本；这些目录是后续产物位置。凭证保留本机环境配置，既不复制现有工程账户也不写入样本。
"""


def overview(records: list[dict]) -> str:
    counts = Counter(p["fidelity"] for p in records)
    rows = [f"| [{p['id']}](./{p['directory']}/design.md) | {p['title']} | {p['family_name']} | L{p['level']} | {p['template_group']} | {p['fidelity']} | planned |" for p in records]
    return rf"""# 100个工业自动化仿真项目设计库

创建日期：{DATE}。已建立**100个独立项目子目录及设计草案**，涵盖10个工艺族、36个拟定模板组。难度分配为L1 20、L2 30、L3 30、L4 20。

当前完成的是整体设计与材料组织。**本库尚未生成或验收100个Factory I/O/CODESYS工程，训练可用项目数为0。** 现有跑通工程保留原位，本轮没有复制或重新启动它们。A类{counts['A']}项拟用原生映射，B类{counts['B']}项有明确抽象或兼容性待试验；A/B都不是完成状态。

- [机器索引catalog.json](catalog.json) / [表格索引catalog.csv](catalog.csv)：100项元数据、项目路径、依赖、状态及来源。
- [分阶段实施与配额curriculum.md](curriculum.md)：由易到难顺序、首批20项试点和前置能力。
- [共享设计规则](shared-design-rules.md)：坐标、物料、IO、PLC、多Agent交接和验收门。
- [来源注册表sources.json](sources.json)：官方能力、工业资料、厂商案例、课程与候选仓库；证据等级分别保留。
- [规划来源调查](../docs/scene-dataset-plan.md)：前期资料搜索与计数口径。
- [完整性报告integrity-report.json](integrity-report.json)：目录、配额、结构草案、引用与JSON检查；不代表物理验收。

每个项目内有：`design.md`需求/拓扑/对象布局/PLC/Agent方案，`project.json`结构化合同，`io-plan.csv`待绑定语义角色，`acceptance-plan.json`待执行用例，`sources.md`来源映射，以及`materials/`下的参考、场景、PLC、证据、训练五类目录。项目方案为自主工艺组合，来源链接不代表100份外部附件已下载。

先完成首批20项的能力验证与验收，再展开其余80项；编号按难度排序，试点为了覆盖工艺可跨级选取。不要同时启动100个现场实现任务。所有项目当前`planned`、验收门`pending`、`sft_eligible=false`。

## 项目总表

| 项目 | 设计名称 | 工艺族 | 难度 | 模板组 | 保真 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
{chr(10).join(rows)}

## 维护

源定义：[scenario_plan_data.py](../tools/scenario_plan_data.py)；模块草案：[scenario_plan_modules.py](../tools/scenario_plan_modules.py)；生成与检查：[build_scenario_project_plans.py](../tools/build_scenario_project_plans.py)。首次生成拒绝已存在目录，避免覆盖后续人工编辑。仅检查可运行：

```powershell
.venv\Scripts\python.exe tools/build_scenario_project_plans.py --validate
```

检查器采用元数据、引用和模块/模式/连接特征去重，并未完成几何图同构、外部谱系或物理可行性证明。增加训练数据前仍需重新审查拓扑与种子谱系。
"""


def curriculum(records: list[dict]) -> str:
    matrix = []
    for f, (name, quota) in FAMILIES.items():
        c = Counter(p["level"] for p in records if p["family"] == f)
        matrix.append(f"| {name} | {c[1]} | {c[2]} | {c[3]} | {c[4]} | {quota} |")
    by_slug = {p["slug"]: p for p in records}
    pilots = []
    for i, slug in enumerate(PILOT_SLUGS, 1):
        p = by_slug[slug]
        pilots.append(f"| {i} | [{p['id']} {p['title']}]({p['directory']}/design.md) | L{p['level']} | {p['template_group']} |")
    return f"""# 配额、实施顺序与交付门

## 十个工艺族与四级难度

| 工艺族 | L1基础 | L2单元协同 | L3多单元 | L4综合恢复 | 合计 |
| --- | --- | --- | --- | --- | --- |
{chr(10).join(matrix)}
| 合计 | 20 | 30 | 30 | 20 | 100 |

36个模板组用于初始谱系管理；100个项目采用不同模块、物理连接、检测/执行配置或工作模式。最终有效场景数在物理实现、外部种子去重和能力核对后确认，不能以这张表提前报告100个可用样本。

## 首批20项试点

这是覆盖基础能力的推荐执行顺序；编号是目录定位，不等于每天进度。试点包含后续组合所需部件，因此跨L1–L4选取。每项依赖见其project.json，依赖通过后再实现。

| 顺序 | 项目 | 难度 | 验证模板 |
| --- | --- | --- | --- |
{chr(10).join(pilots)}

重量二路项目用于对照当前已有基线，仍需把场景、PLC、绑定和证据归档后才能算本库已验收项目。既有场景不额外算第101项；如果与本库结构重复，选作种子而非重复累计。

## 分阶段扩展

1. 能力与模板阶段：清点本机Prefab/模式，完成坐标、标签、协议表示和单件闭环标定；把已验证部件扩展到现有component-semantics知识库。当前知识库只覆盖部分部件，不能声称已支持本库全部100项目。
2. 试点阶段：按上表跑通20项，形成最小生成→加载→视觉修复→PLC→协议→工艺验收链，每项保留完整材料与失败样本。
3. 扩展阶段：剩余80项按L1→L2→L3→L4及依赖完成；同模板复用经验证模块，但重新检查有效结构差异、坐标和IO。
4. 数据阶段：人工审核规划、代码与轨迹，明确许可和谱系，再制作各角色LoRA-SFT数据；100工程数量不能直接换算训练样本数量。

## 每项目交付定义

规划交付：需求、对象及模式、拓扑、保真边界、IO语义草案、PLC思路、独立验收与来源。当前100项达到规划材料生成，尚未经过逐份人工审定。

实现交付：生成代码与.factoryio可加载，完整原生IO绑定，ST/GVL和CODESYS.project可编译；有本机能力和单位校准记录。

运行交付：下载与Run、协议读回、Factory I/O运行都分别有证据，功能/并发/恢复用例执行，确定性验收和Agent3多视角验收均通过。

训练交付：角色样本有真实输入输出与证据引用，人工复核、许可和谱系划分完成。规划库无这些产物时训练资格保持false。

## 多Agent任务粒度

以一个项目的一个版本为任务单元：Agent1提交设计合同；Agent2生成实例；Agent3按证据分发几何/控制问题；PLC Agent接收冻结IO；工具层执行和保存验收。先给每个工艺族建立模板，再允许不同项目并行构建。共享Factory I/O/CODESYS实例的加载、下载和运行使用独占锁，避免不同项目互相覆盖；离线规划与代码准备可以并行。
"""


def validate_library() -> dict:
    catalog = json.loads((OUTPUT / "catalog.json").read_text(encoding="utf-8"))
    entries = catalog["projects"]
    with (OUTPUT / "catalog.csv").open(encoding="utf-8-sig", newline="") as f:
        index_rows = list(csv.DictReader(f))
    assert [e["id"] for e in index_rows] == [e["id"] for e in entries]
    records = []
    for entry in entries:
        directory = OUTPUT / entry["directory"]
        p = json.loads((directory / "project.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256((directory / "project.json").read_bytes()).hexdigest()
        assert digest == entry["project_sha256"], f"Catalog needs refresh: {entry['id']}"
        assert p["id"] == entry["id"]
        assert p["status"] == "planned" and not p["training"]["sft_eligible"]
        assert set(p["gates"].values()) == {"pending"}
        assert not p["required_bindings_complete"]
        for r in p["io_roles"]:
            assert all(r[k] is None for k in ["object_guid", "io_guid", "point_io_key", "driver_channel", "tag_address"])
        for filename in ["design.md", "sources.md", "io-plan.csv", "acceptance-plan.json", "materials/README.md"]:
            assert (directory / filename).is_file(), filename
        actual_cases = json.loads((directory / "acceptance-plan.json").read_text(encoding="utf-8"))
        assert actual_cases["cases"] == p["acceptance_cases"]
        assert all(c["status"] == "planned" and not c["evidence_paths"] for c in actual_cases["cases"])
        with (directory / "io-plan.csv").open(encoding="utf-8-sig", newline="") as f:
            signals = list(csv.DictReader(f))
        assert [r["symbol"] for r in signals] == [r["symbol"] for r in p["io_roles"]]
        for path in p["artifact_paths"].values():
            assert (directory / path).is_dir()
            assert not list((directory / path).iterdir()), f"Unexpected pre-existing implementation: {directory / path}"
        records.append(p)
    validate_inputs(records)
    project_dirs = [d for d in OUTPUT.iterdir() if d.is_dir() and re.match(r"SC\d{3}_", d.name)]
    assert {d.name for d in project_dirs} == {p["directory"] for p in records}
    by_id = {p["id"]: p for p in records}
    for p in records:
        for dependency in p["dependencies"]:
            assert dependency in by_id and by_id[dependency]["level"] < p["level"]
    pilot_by_slug = {p["slug"]: p for p in records if p["slug"] in PILOT_SLUGS}
    assert len(PILOT_SLUGS) == len(pilot_by_slug) == 20
    pilot_positions = {pilot_by_slug[s]["id"]: i for i, s in enumerate(PILOT_SLUGS)}
    for p in pilot_by_slug.values():
        assert all(d in pilot_positions and pilot_positions[d] < pilot_positions[p["id"]] for d in p["dependencies"]), p["id"]
    links_checked = 0
    for path in OUTPUT.rglob("*.md"):
        for link in re.findall(r"\[[^\]]*\]\(([^\)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in link or link.startswith("#"):
                continue
            target = (path.parent / link.split("#")[0]).resolve()
            assert target.exists(), f"Broken link: {path}: {link}"
            links_checked += 1
    return {
        "schema_version": 1, "checked_date": DATE, "status": "passed_design_library_integrity",
        "project_directories": len(project_dirs), "families": dict(Counter(p["family"] for p in records)),
        "levels": dict(Counter(p["level"] for p in records)), "template_groups": len({p["template_group"] for p in records}),
        "distinct_module_mode_connection_signatures": len({signature(p) for p in records}),
        "fidelity": dict(Counter(p["fidelity"] for p in records)),
        "acceptance_cases_planned": sum(len(p["acceptance_cases"]) for p in records),
        "symbolic_io_roles_planned": sum(len(p["io_roles"]) for p in records), "local_links_checked": links_checked,
        "project_json_hashes_valid": True, "io_roles_unbound": True,
        "compound_job_bootstrap_valid": True, "pilot_dependencies_in_order": True,
        "new_runnable_projects_generated": 0, "runtime_acceptance_performed": False,
        "training_eligible_projects": 0,
        "limitations": ["No geometric graph-isomorphism or provenance deduplication proof",
                        "No software loading, PLC generation/compilation/download or simulation",
                        "Source URL reachability and attachments are not checked by this integrity validator"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="read-only integrity check; no files changed")
    args = parser.parse_args()
    if args.validate:
        print(json.dumps(validate_library(), ensure_ascii=False, indent=2))
        return
    records = build_records()
    if OUTPUT.exists():
        raise SystemExit(f"Refusing to overwrite existing design library: {OUTPUT}")
    OUTPUT.mkdir()
    print(json.dumps(materialize(records), ensure_ascii=False, indent=2))


def materialize(records: list[dict]) -> dict:
    """Write known authored files. main guards first creation; never delete unrelated files."""
    by_id = {p["id"]: p for p in records}
    for p in records:
        directory = OUTPUT / p["directory"]
        directory.mkdir(exist_ok=True)
        for name in ["references", "scene", "plc", "evidence", "training"]:
            (directory / "materials" / name).mkdir(parents=True, exist_ok=True)
        dump(directory / "project.json", p)
        dump(directory / "acceptance-plan.json", {"project_id": p["id"], "version": 1, "status": "planned", "cases": p["acceptance_cases"]})
        write_csv(directory / "io-plan.csv", ["symbol", "node", "controller_direction", "planned_type", "role", "origin", "binding_status", "object_guid", "io_guid", "point_io_key", "driver_channel", "tag_address", "polarity", "scale"], p["io_roles"])
        (directory / "design.md").write_text(project_markdown(p, by_id), encoding="utf-8")
        (directory / "sources.md").write_text(source_markdown(p), encoding="utf-8")
        (directory / "materials" / "README.md").write_text(materials_markdown(p), encoding="utf-8")
    entries = []
    for p in records:
        entry = {key: p[key] for key in ["id", "slug", "title", "family", "family_name", "level", "level_name", "template_group", "template_name", "directory", "status", "fidelity", "fidelity_note", "dependencies", "structural_difference", "sources"]}
        entry.update({"project_json": f"{p['directory']}/project.json", "design_md": f"{p['directory']}/design.md",
                      "project_sha256": hashlib.sha256((OUTPUT / p["directory"] / "project.json").read_bytes()).hexdigest(), "sft_eligible": False})
        entries.append(entry)
    dump(OUTPUT / "catalog.json", {"schema_version": 1, "created_date": DATE, "status": "planned", "projects": entries})
    csv_entries = [{**e, "dependencies": ";".join(e["dependencies"]), "sources": ";".join(e["sources"])} for e in entries]
    write_csv(OUTPUT / "catalog.csv", ["id", "title", "family_name", "level", "level_name", "template_group", "fidelity", "status", "directory", "dependencies", "structural_difference", "sources", "sft_eligible"], csv_entries)
    dump(OUTPUT / "sources.json", {"schema_version": 1, "investigation_date": DATE, "attachments_downloaded_this_turn": False, "sources": SOURCES})
    (OUTPUT / "README.md").write_text(overview(records), encoding="utf-8")
    (OUTPUT / "curriculum.md").write_text(curriculum(records), encoding="utf-8")
    (OUTPUT / "shared-design-rules.md").write_text(shared_rules(), encoding="utf-8")
    dump(OUTPUT / "integrity-report.json", {"status": "pending_design_library_integrity"})
    report = validate_library()
    dump(OUTPUT / "integrity-report.json", report)
    return report


if __name__ == "__main__":
    main()
