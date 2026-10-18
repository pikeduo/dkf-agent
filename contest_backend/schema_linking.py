"""选择性复用参考包的目录匹配/别名/外键闭包思路，歧义不自动选首项。"""

import hashlib
import re
from dataclasses import dataclass, field

from contracts.contest_v1 import LinkEvidence, SchemaCandidate, SchemaLink
from contest_backend.errors import ContestError
from contest_backend.registry import ForeignKey, SourceSpec


def tokens(text: str) -> set[str]:
    """复用参考TableMatcher的驼峰/词形拆分，避免空词命中全部表。"""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {normalized for word in re.findall(r"[A-Za-z]+|[\u4e00-\u9fff]+|[0-9]+", text)
            if (normalized := word.lower().removesuffix("s"))}


def stable_id(text: str) -> str:
    """为目录引用生成短稳定标识，不将私有路径作为证据标识。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass
class LinkResult:
    """Linking中间态只在当前会话内使用；公开依据转换为冻结SchemaLink。"""

    links: list[SchemaLink] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    keys: list[ForeignKey] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    selected_columns: set[str] = field(default_factory=set)


def candidate(source: SourceSpec, target: str, kind: str, reference: str) -> SchemaCandidate:
    """候选只从已验证目录创建，完整保留表列和来源记录。"""
    table, _, column = target.partition(".")
    return SchemaCandidate(candidate_id=stable_id(target), schema_name=source.schema_name, table=table,
                           column=column or None, evidence=[LinkEvidence(kind=kind, reference_id=stable_id(reference),
                                                                        catalog_fingerprint=source.fingerprint())])


def find_paths(source: SourceSpec, start: str, end: str) -> list[list[ForeignKey]]:
    """枚举至多四跳的真实键路径，保留多约束歧义；预算超限拒绝猜路径。"""
    queue, result, inspected = [(start, {start}, [])], [], 0
    while queue:
        node, visited, path = queue.pop(0)
        inspected += 1
        if inspected > 512 or len(result) > 20:
            raise ContestError("RESULT_INVALID", 422)
        if node == end:
            result.append(path)
            continue
        if len(path) == 4:
            continue
        for key in source.foreign_keys:
            neighbor = key.to_table if key.from_table == node else key.from_table if key.to_table == node else None
            if neighbor is not None and neighbor not in visited:
                queue.append((neighbor, visited | {neighbor}, path + [key]))
    return result


class SchemaLinker:
    """无模型、无值探查；真实目录不足或多义时生成待确认问题。"""

    def link(self, source: SourceSpec, question: str, confirmed_slots: dict[str, str]) -> LinkResult:
        """明确消费已确认条件；同义别名合并，字段与连接路径选择均可追溯。"""
        result, matches = LinkResult(), {}
        for alias, targets in source.aliases.items():
            if alias.casefold() in question.casefold():
                matches[alias] = list(dict.fromkeys(targets))
        matching_text = question
        for condition in source.conditions:
            # 明确过滤赋值不是结果列选择，避免COUNT被迫投影年度等过滤字段。
            matching_text = re.sub(r"(?:^|[\s,，;；])" + re.escape(condition.slot)
                                   + r"=[^\s,，;；]{1,1000}", " ", matching_text)
        words = tokens(matching_text)
        for table in source.table_map():
            if tokens(table) & words:
                matches.setdefault(table, [table])
        # 显式表可以缩小同名列候选，但不能因找到表而漏掉问题中的字段歧义。
        explicit_tables = {targets[0] for targets in matches.values()
                           if len(targets) == 1 and targets[0] in source.table_map()}
        for table, columns in source.table_map().items():
            if explicit_tables and table not in explicit_tables:
                continue
            for column in columns:
                if tokens(column) & words and column not in source.aliases:
                    target = f"{table}.{column}"
                    if target not in matches.setdefault(column, []):
                        matches[column].append(target)
        if not matches:
            if len(source.tables) > 20:
                raise ContestError("UNSUPPORTED", 422)
            result.questions.append({"slot": "source_target", "prompt": "请明确本次查询的表或字段",
                                     "choices": {stable_id(table): table for table in source.table_map()}})
            chosen = confirmed_slots.get("source_target")
            if chosen in source.table_map():
                matches[chosen] = [chosen]
                result.questions.clear()
        for mention, targets in matches.items():
            if len(targets) > 20:
                raise ContestError("UNSUPPORTED", 422)
            slot = "schema_" + stable_id(mention)
            chosen = confirmed_slots.get(slot)
            if chosen is not None and chosen not in targets:
                raise ContestError("SCOPE_INVALID", 403)
            selected = chosen if chosen is not None else targets[0] if len(targets) == 1 else None
            items = [candidate(source, target, "alias" if mention in source.aliases else "schema", mention) for target in targets]
            result.links.append(SchemaLink(mention=mention, state="selected" if selected else "ambiguous",
                                            candidates=items, selected_id=stable_id(selected) if selected else None))
            if selected is None:
                result.questions.append({"slot": slot, "prompt": f"请确认“{mention}”指哪个字段或口径",
                                         "choices": {stable_id(target): target for target in targets}})
            else:
                result.tables.add(selected.split(".")[0])
                if "." in selected:
                    result.selected_columns.add(selected)
        for condition in source.conditions:
            explicit_values = re.findall(r"(?:^|[\s,，;；])" + re.escape(condition.slot)
                                         + r"=([^\s,，;；]{1,1000})", question)
            if (condition.required or condition.slot in confirmed_slots or explicit_values
                    or any(term in question for term in condition.triggers)):
                result.tables.add(condition.table)
                if condition.slot not in confirmed_slots:
                    # 仅识别明确的“slot=值”；其余自然语言中的数值不按位置猜绑定。
                    explicit = explicit_values[0] if len(set(explicit_values)) == 1 else None
                    result.questions.append({"slot": condition.slot, "prompt": condition.label,
                                             "choices": {}, "explicit": explicit})
                else:
                    target = f"{condition.table}.{condition.column}"
                    result.links.append(SchemaLink(mention=condition.label, state="selected",
                        candidates=[candidate(source, target, "confirmed_slot", condition.slot)], selected_id=stable_id(target)))
        selected_tables = sorted(result.tables)
        if not result.questions and selected_tables:
            for target in selected_tables[1:]:
                paths = find_paths(source, selected_tables[0], target)
                slot = "join_" + stable_id(selected_tables[0] + ":" + target)
                choices = {stable_id("/".join(key.id for key in path)): path for path in paths}
                chosen = confirmed_slots.get(slot)
                if not choices:
                    # 没有真实外键依据时不能把自由文本关系当成已验证关系。
                    if chosen == "cancel":
                        raise ContestError("UNSUPPORTED", 422)
                    result.questions.append({"slot": slot, "prompt": "没有可验证的外键路径；请结束本次问数后缩小范围，或由管理员补齐真实关系目录",
                                             "choices": {"cancel": "cancel"}})
                    continue
                if chosen is None and len(paths) > 1:
                    result.questions.append({"slot": slot, "prompt": "存在多个真实外键路径，请确认连接依据",
                                             "choices": {key: "/".join(item.id for item in path) for key, path in choices.items()}})
                    continue
                path = next((path for path in paths if "/".join(key.id for key in path) == chosen), None) if chosen else paths[0]
                if path is None:
                    raise ContestError("SCOPE_INVALID", 403)
                for key in path:
                    if key not in result.keys:
                        result.keys.append(key)
                        result.tables.update((key.from_table, key.to_table))
                        result.links.append(SchemaLink(mention=key.id, state="selected",
                            candidates=[candidate(source, f"{key.from_table}.{key.pairs[0][0]}", "foreign_key", key.id)],
                            selected_id=stable_id(f"{key.from_table}.{key.pairs[0][0]}")))
        if len(result.questions) > 32 or len(result.links) > 128:
            raise ContestError("UNSUPPORTED", 422)
        return result
