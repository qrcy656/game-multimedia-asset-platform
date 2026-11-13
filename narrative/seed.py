"""Small editable templates; no server-side generation or model dependencies."""


def legacy_blank(title="新的故事"):
    return {"schema_version": 1, "title": title, "description": "", "boards": [{"id": "chapter1", "name": "第一章"}], "nodes": [], "edges": [], "variables": [], "characters": [], "assets": [], "start_node_id": ""}


def legacy_snow(title="雪原来信"):
    """Exact v1 fingerprint, used only to migrate an untouched built-in example."""
    doc = legacy_blank(title)
    doc["description"] = "从雪原入口到冰原狼的秘密：修改选择、勇气变量和自动分支，试玩不同的路线。"
    doc["boards"] = [{"id": "chapter1", "name": "雪原入口"}, {"id": "chapter2", "name": "冰湖结局"}]
    doc["variables"] = [{"id": "courage", "name": "勇气", "type": "number", "initial": 0}, {"id": "supplies", "name": "补给", "type": "number", "initial": 1}, {"id": "trust", "name": "获得信任", "type": "boolean", "initial": False}]
    doc["characters"] = [{"id": "eros", "name": "Eros", "description": "带着一封无名来信踏入雪原的旅行者。", "color": "#54b7a7", "asset_id": ""}, {"id": "wolf", "name": "冰原狼", "description": "守护冰湖的小狼，正在寻找失落的铃铛。", "color": "#8da9e8", "asset_id": ""}]

    def node(node_id, title, body, x, y, kind="scene", board="chapter1", speaker="", actions=None):
        return {"id": node_id, "board_id": board, "type": kind, "position": {"x": x, "y": y}, "title": title, "body": body, "speaker": speaker, "color": "#b692ed" if kind == "branch" else "#d5af63" if kind == "note" else "#54b7a7", "asset_id": "", "actions": actions or []}

    doc["nodes"] = [
        node("arrival", "抵达雪原", "风雪吞没了来时的脚印。信封上只有一句话：请在黎明前，找到冰湖边的铃声。", 0, 150),
        node("fork", "林间岔路", "左边传来低沉的狼鸣，右边亮着废弃驿站的灯。\n当前补给：{{补给}}。你决定如何前进？", 340, 150, "dialogue", speaker="eros"),
        node("follow", "追随狼鸣", "你握紧信封，向幽蓝的森林迈出一步。风里似乎藏着求救的声音。", 680, 0, actions=[{"variable": "courage", "op": "add", "value": 1}]),
        node("station", "寻找补给", "驿站里留着温热的茶和一张旧地图。你补充了食物，沿安全的小径前往冰湖。", 680, 300, actions=[{"variable": "supplies", "op": "add", "value": 1}]),
        node("test", "是否直面冰原狼？", "自动检查勇气。连线顺序决定条件判断优先级，无条件连线用于兜底。", 1020, 150, "branch"),
        node("meeting", "冰原狼的请求", "冰原狼把一串断开的铃绳推向你。\n“愿意帮我找回铃铛吗？湖底的声音，只有你听得见。”", 1360, 0, "dialogue", speaker="wolf"),
        node("shore", "远处的铃声", "你绕过森林，在湖岸听到了细碎的铃声。地图背面浮现出一行字：勇敢不只意味着向前。", 1360, 300),
        node("ending", "黎明的约定", "第一缕阳光落在冰面。故事尚未结束，你已经选择了自己的方向。\n\n勇气：{{勇气}}\n剩余补给：{{补给}}\n获得冰原狼信任：{{获得信任}}", 100, 150, board="chapter2"),
        node("memo", "设计便笺", "拖动节点整理剧情；从节点右侧连接点拉出新分支。选择连线编辑选项与条件。\n\n试玩两条路线，观察变量与跨画板跳转。便笺不参与试玩。", 0, 470, "note"),
    ]

    def edge(edge_id, source, target, label, conditions=None, actions=None):
        return {"id": edge_id, "source": source, "target": target, "label": label, "conditions": conditions or [], "actions": actions or []}

    doc["edges"] = [
        edge("e1", "arrival", "fork", "走进森林"),
        edge("e2", "fork", "follow", "追随狼鸣 · 勇气 +1"),
        edge("e3", "fork", "station", "寻找补给 · 补给 +1"),
        edge("e4", "follow", "test", "抵达冰湖"),
        edge("e5", "station", "test", "沿小径抵达冰湖"),
        edge("e6", "test", "meeting", "勇气 ≥ 1", [{"variable": "courage", "op": "gte", "value": 1}]),
        edge("e7", "test", "shore", "其他情况"),
        edge("e8", "meeting", "ending", "答应寻找铃铛", actions=[{"variable": "trust", "op": "set", "value": True}]),
        edge("e9", "meeting", "ending", "先留下补给，再独自探索", [{"variable": "supplies", "op": "gte", "value": 1}], [{"variable": "supplies", "op": "add", "value": -1}]),
        edge("e10", "shore", "ending", "记下铃声，等待黎明"),
    ]
    doc["start_node_id"] = "arrival"
    return doc


def blank(title="新的故事"):
    return {"schema_version": 3, "title": title, "description": "", "boards": [{"id": "chapter1", "name": "主线大纲"}], "nodes": [], "edges": [], "characters": [], "assets": [], "start_node_id": ""}


def snow(title="雪原来信"):
    doc = blank(title)
    doc["description"] = "用主线串起事件，用支线补充人物动机与世界线索。编辑节点和连接说明，整理雪原故事的叙事结构。"
    doc["boards"] = [{"id": "main", "name": "主线 · 雪原来信"}, {"id": "side", "name": "支线 · 失落铃铛"}]
    doc["characters"] = [{"id": "eros", "name": "Eros", "description": "带着一封无名来信踏入雪原的旅行者。主线动机是找到写信人，支线逐步揭示他对雪原的理解。", "color": "#54b7a7", "asset_id": ""}, {"id": "wolf", "name": "冰原狼", "description": "守护冰湖的小狼。寻找失落铃铛的经历，使它成为主角与雪原旧事之间的联系。", "color": "#8da9e8", "asset_id": ""}]

    def node(node_id, title, body, x, y, kind="scene", board="main", speaker=""):
        dialogue = [{"id": "line_1", "speaker": speaker, "body": body}] if kind == "dialogue" else []
        return {"id": node_id, "board_id": board, "type": kind, "position": {"x": x, "y": y}, "title": "" if dialogue else title, "body": "" if dialogue else body, "speaker": "" if dialogue else speaker, "color": "#b692ed" if kind == "branch" else "#d5af63" if kind == "note" else "#54b7a7", "asset_id": "", "substory": None, "dialogue": dialogue, "prerequisites": []}

    doc["nodes"] = [
        node("arrival", "开篇：风雪中的来信", "Eros 收到没有署名的信，来到封闭多年的雪原。信中反复提到冰湖与铃声。\n叙事作用：建立主角目标，留下写信人身份的悬念。", 0, 130),
        node("station", "废弃驿站的线索", "驿站里留下旧地图、断裂的铃绳和一段未完成的留言。Eros 得知冰湖曾经连接着一座失落的村庄。\n叙事作用：交代世界背景，把行动目标引向冰湖。", 340, 130),
        node("threads", "主线与支线的展开", "主线沿旧地图前往冰湖，追查来信的来源。\n支线追随林中的狼鸣，了解铃绳的主人。两条叙事线最终在冰湖边汇合。", 680, 130, "branch"),
        node("lake", "冰湖下的村庄", "Eros 在冰面下看见旧村的轮廓。湖岸的文字与来信笔迹一致；冰原狼的铃铛也属于同一个年代。\n叙事作用：让主线谜题与支线人物线索相互印证。", 1100, 130),
        node("ending", "章末：写信人的约定", "黎明到来，冰湖上传出久违的铃声。Eros 得知来信是一场尚未完成的约定，并决定寻找村庄的幸存者。\n叙事作用：回答本章的部分悬念，接续下一章的目标。", 1450, 130),
        node("wolf", "", "你手里的铃绳，是从驿站找到的吗？它原本系着旧村的铃铛。能帮我去林间营地找回来吗？", 0, 140, "dialogue", board="side", speaker="wolf"),
        node("camp", "支线发展：埋在雪下的营地", "Eros 沿旧营地的痕迹找到铃铛，也发现写信人曾经照顾过冰原狼。\n支线补充：通过一个具体人物经历，让失落村庄的历史变得可感知。", 360, 140, board="side"),
        node("return", "支线回收：铃声指向冰湖", "铃铛归还后，冰原狼带 Eros 走向湖岸。它对旧村的记忆，为主线中的地图和来信提供了新的解释。\n回收位置：与主线“冰湖下的村庄”汇合。", 720, 140, board="side"),
        node("memo", "梳理提示", "场景节点记录事件，对话节点记录人物表达，分支节点标记主支线的展开或汇合。\n连接上的说明记录前置剧情、发生缘由或衔接方式。\n可继续补充：关键伏笔、人物动机、尚未回收的线索。", 20, 470, "note"),
    ]
    request = next(item for item in doc["nodes"] if item["id"] == "wolf")
    request["dialogue"].append({"id": "line_2", "speaker": "eros", "body": "我会去找。也许那只铃铛能告诉我，寄来这封信的人去了哪里。"})
    request["prerequisites"] = [{"node_id": "station", "description": "在废弃驿站发现断裂的铃绳，再向冰原狼询问。"}]

    def edge(edge_id, source, target, label, requirements=""):
        return {"id": edge_id, "source": source, "target": target, "label": label, "requirements": requirements, "source_handle": "right", "target_handle": "left"}

    doc["edges"] = [
        edge("e1", "arrival", "station", "主线推进", "Eros 依照信中的地址进入雪原。"),
        edge("e2", "station", "threads", "发现两组线索", "旧地图指向冰湖；铃绳与林中的狼鸣形成支线引子。"),
        edge("e3", "threads", "lake", "继续主线", "Eros 决定先追查信件的来源。"),
        edge("e4", "threads", "wolf", "展开冰原狼支线", "Eros 留意到狼鸣与断裂铃绳之间的联系。"),
        edge("e5", "wolf", "camp", "调查旧营地", "明确冰原狼寻找铃铛的缘由。"),
        edge("e6", "camp", "return", "回收支线线索", "铃铛被找回，写信人与冰原狼的关系得到揭示。"),
        edge("e7", "return", "lake", "汇入主线", "支线提供的记忆补充冰湖与旧村的背景。"),
        edge("e8", "lake", "ending", "收束本章", "将来信、铃铛与失落村庄的线索联系起来。"),
    ]
    doc["start_node_id"] = "arrival"
    return doc
