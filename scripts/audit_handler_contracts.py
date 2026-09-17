from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "app" / "handlers"

callback_routes: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
callback_handlers = 0
callback_without_answer: list[tuple[str, int, str]] = []
message_handlers = 0
fsm_state_handlers = 0

# Mimoru still contains compatibility/legacy handlers with identical filters.
# Aiogram uses router registration order, so this map turns that implicit
# dependency into an explicit tested contract. New duplicates or a changed
# winner fail CI instead of silently changing user-visible behavior.
EXPECTED_DUPLICATE_WINNERS = {
    "F.data == 'panel:commands'": "home_panel.guided_help",
    "F.data == 'panel:groups'": "group_directory.user_groups",
    "F.data == 'panel:home'": "home_panel.guided_home",
    "F.data == 'panel:my_stats'": "home_panel.choose_group_statistics",
    "F.data == 'panel:plans'": "plan_directory.catalog",
    "F.data == 'plans_catalog:compare'": "plan_directory.compare",
    "F.data == 'service:groups'": "group_directory.service_groups_all",
    "F.data == 'service:subscriptions'": "service_management.subscriptions",
    "F.data.regexp('^admin_access_apply:\\\\d+:\\\\d+:[a-z_]+:(telegram|bot_only)$')": "rank_provisioning_handlers.safe_admin_access_apply",
    "F.data.regexp('^channels:\\\\d+$')": "navigation.required_subscriptions",
    "F.data.regexp('^gpost:(approve|reject):\\\\d+$')": "ad_market_atomic.atomic_global_review",
    "F.data.regexp('^group:\\\\d+$')": "group_directory.user_group_card",
    "F.data.regexp('^group_disconnect_do:\\\\d+$')": "group_onboarding_flow.disconnect_group_crash_safe",
    "F.data.regexp('^logs:\\\\d+$')": "navigation_fixes.moderation_logs_with_contextual_back",
    "F.data.regexp('^logs:\\\\d+:page:\\\\d+$')": "navigation_fixes.moderation_logs_with_contextual_back",
    "F.data.regexp('^member_card:\\\\d+:-?\\\\d+$')": "member_navigation.member_card_context",
    "F.data.regexp('^member_history:\\\\d+:-?\\\\d+$')": "navigation_fixes.member_history_with_contextual_back",
    "F.data.regexp('^members_stats:\\\\d+$')": "navigation_fixes.member_activity_with_contextual_back",
    "F.data.regexp('^modreason:[0-9a-f]{10}:\\\\d+$')": "moderation_durable_guard.durable_reason_action",
    "F.data.regexp('^plan:\\\\d+$')": "plan_directory.group_plan",
    "F.data.regexp('^plans_apply:(standard|pro):\\\\d+:(catalog|group)$')": "plan_directory.plan_for_group",
    "F.data.regexp('^plans_catalog:(free|standard|pro)$')": "plan_directory.catalog_detail",
    "F.data.regexp('^plans_choose_group:(standard|pro)$')": "plan_directory.choose_group_by_reference",
    "F.data.regexp('^rank_add_choose:\\\\d+:[a-z_]+$')": "admin_access_mode.rank_add_choose_mode",
    "F.data.regexp('^rank_change:\\\\d+:\\\\d+:[a-z_]+$')": "rank_provisioning_handlers.safe_rank_change",
    "F.data.regexp('^rank_quick:\\\\d+:\\\\d+:[a-z_]+$')": "admin_access_mode.rank_quick_choose_mode",
    "F.data.regexp('^rank_remove:\\\\d+:\\\\d+$')": "rank_provisioning_handlers.safe_rank_remove",
    "F.data.regexp('^reqdeal:(accept|reject):\\\\d+$')": "ad_market_atomic.atomic_required_deal_decision",
    "F.data.regexp('^reqlist:toggle:\\\\d+$')": "ad_market_atomic.atomic_required_listing_toggle",
    "F.data.regexp('^role_add:\\\\d+$')": "telegram_roles.rank_add",
    "F.data.regexp('^role_edit:\\\\d+:\\\\d+$')": "telegram_roles.rank_edit",
    "F.data.regexp('^role_remove:\\\\d+:\\\\d+$')": "rank_provisioning_handlers.safe_rank_remove",
    "F.data.regexp('^role_remove_confirm:\\\\d+:\\\\d+$')": "telegram_roles.rank_remove_confirm",
    "F.data.regexp('^roles:\\\\d+$')": "telegram_roles.telegram_and_solivra_roles",
    "F.data.regexp('^service:groups:(active|disabled)$')": "group_directory.service_groups_filtered",
    "F.data.regexp('^service_client:\\\\d+$')": "contextual_back.service_client_context",
    "F.data.regexp('^service_group:\\\\d+$')": "contextual_back.service_group_context",
    "F.data.regexp('^service_group_action:\\\\d+:(enable|disable)$')": "group_directory.service_group_action",
    "F.data.regexp('^service_group_confirm:\\\\d+:(enable|disable)$')": "group_directory.service_group_confirm",
    "F.data.regexp('^service_plan:\\\\d+$')": "service_management.service_plan",
    "F.data.regexp('^service_plan_action:\\\\d+:(free|trial|standard|pro):(0|7|30)$')": "service_management_fixes.service_plan_action_fixed",
    "F.data.regexp('^service_plan_apply:\\\\d+:(trial|standard|pro|free):(0|7|30)$')": "service_management_fixes.service_plan_apply_serialized",
    "F.data.regexp('^service_plan_grant:\\\\d+:(free|trial|standard|pro):(0|7|30)$')": "service_management_fixes.service_plan_grant_serialized",
    "F.data.regexp('^setting_flood:\\\\d+:(4|6|8):(5|10|15)$')": "navigation_fixes.contextual_antiflood_set",
    "F.data.regexp('^setup:\\\\d+:start$')": "wizard_navigation.legacy_start",
}


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def decorator_kind(dec: ast.expr) -> tuple[str | None, str | None]:
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None, None
    attr = dec.func.attr
    if attr not in {"callback_query", "message", "chat_join_request", "my_chat_member", "pre_checkout_query"}:
        return None, None
    parts = [ast.unparse(arg) for arg in dec.args]
    parts.extend(f"{kw.arg}={ast.unparse(kw.value)}" for kw in dec.keywords)
    route = ", ".join(parts) if parts else "<any>"
    return attr, route


def has_callback_answer(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Await) or not isinstance(child.value, ast.Call):
            continue
        call = child.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "answer":
            owner = call.func.value
            if isinstance(owner, ast.Name) and owner.id in {"callback", "query", "event"}:
                return True
    for child in ast.walk(node):
        if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
            func = child.value.func
            if isinstance(func, ast.Name) and func.id not in {"sleep", "commit", "flush", "execute", "scalar", "scalars"}:
                return True
    return False


def router_order() -> dict[str, int]:
    text = (ROOT / "app/main.py").read_text(encoding="utf-8")
    match = re.search(r"dp\.include_routers\((.*?)\n\s*\)\n", text, re.S)
    if not match:
        raise SystemExit("Cannot locate dp.include_routers() in app/main.py")
    names = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.router\b", match.group(1))
    return {name: index for index, name in enumerate(names)}


for path in sorted(HANDLERS.glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel(path))
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        kinds: list[tuple[str, str]] = []
        for dec in node.decorator_list:
            kind, route = decorator_kind(dec)
            if kind:
                kinds.append((kind, route or "<any>"))
        for kind, route in kinds:
            if kind == "callback_query":
                callback_handlers += 1
                callback_routes[route].append((rel(path), node.lineno, node.name))
                if isinstance(node, ast.AsyncFunctionDef) and not has_callback_answer(node):
                    callback_without_answer.append((rel(path), node.lineno, node.name))
            elif kind == "message":
                message_handlers += 1
                if "State" in route or "Form." in route:
                    fsm_state_handlers += 1


duplicates = {route: items for route, items in callback_routes.items() if len(items) > 1}
errors: list[str] = []
orders = router_order()

actual_routes = set(duplicates)
expected_routes = set(EXPECTED_DUPLICATE_WINNERS)
for route in sorted(actual_routes - expected_routes):
    errors.append(f"unexpected duplicate callback filter: {route}")
for route in sorted(expected_routes - actual_routes):
    errors.append(f"expected legacy duplicate disappeared; review allowlist: {route}")

print(
    f"Handler contract inventory: {callback_handlers} callback handlers, "
    f"{message_handlers} message handlers, {fsm_state_handlers} FSM message handlers"
)
print(f"Exact duplicate callback filters: {len(duplicates)} (all must have locked winners)")
for route, items in sorted(duplicates.items()):
    locations = ", ".join(f"{path}:{line}:{name}" for path, line, name in items)
    print(f"DUP {route} -> {locations}")
    ranked: list[tuple[int, str]] = []
    for path, _line, name in items:
        module = Path(path).stem
        if module not in orders:
            errors.append(f"duplicate handler router {module}.router is not registered in app/main.py")
            continue
        ranked.append((orders[module], f"{module}.{name}"))
    if ranked:
        winner = min(ranked)[1]
        expected = EXPECTED_DUPLICATE_WINNERS.get(route)
        if expected is not None and winner != expected:
            errors.append(f"duplicate winner changed for {route}: expected {expected}, got {winner}")
        print(f"WIN {route} -> {winner}")

print(f"Callback handlers requiring manual answer/delegation review: {len(callback_without_answer)}")
for path, line, name in callback_without_answer:
    print(f"REVIEW {path}:{line}:{name}")

if callback_without_answer:
    errors.append(
        f"{len(callback_without_answer)} callback handler(s) have no visible answer/delegation; Telegram spinner may remain active"
    )

if errors:
    for item in errors:
        print("ERROR", item)
    raise SystemExit(f"Handler contract audit failed with {len(errors)} error(s)")

print("Handler contract audit: OK; legacy duplicate precedence is locked")
