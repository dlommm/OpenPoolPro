"""The staff navigation: one model, drawn three ways by base.html.

Desktop draws it as the masthead's group tabs with the active group's pages
in a second row (or as a grouped sidebar, when the company picked that
layout); tablet and phone draw it as the menu sheet plus a five-slot bottom
bar on phones. Visibility mirrors each route's own permission dependency --
hiding an item is a convenience, the server still checks every request.
"""
from dataclasses import dataclass, field


def _admin(u):
    return u is None or u.role == "admin"


def _perm(flag):
    return lambda u: u is None or u.can(flag)


def _billing(u):
    return u is None or u.can("view_money") or u.can("approve_billing")


def _all(u):
    return True


@dataclass(frozen=True)
class Item:
    key: str
    href: str
    label: str
    icon: str
    show: object = _all
    # Other paths that belong to this page (detail views, sub-pages).
    also: tuple = ()


@dataclass(frozen=True)
class Group:
    label: str  # "" = Home
    items: tuple


OWNER = (
    Group("Home", (Item("dash", "/dashboard", "Dashboard", "dash", _admin),)),
    Group("Field", (
        Item("today", "/", "Today", "today", also=("/visit/", "/today/")),
        Item("week", "/week", "Week", "week"),
    )),
    Group("Customers", (
        Item("customers", "/customers", "Customers", "users",
             also=("/properties/", "/pools/", "/search")),
        Item("jobs", "/jobs", "Service jobs", "wrench"),
        Item("quotes", "/quotes", "Quotes", "quote", _perm("manage_customers")),
        Item("contracts", "/contracts", "Contracts", "pen", _perm("manage_customers")),
    )),
    Group("Money", (
        Item("invoices", "/invoices", "Invoices", "file", _admin),
        Item("billing", "/billing", "QuickBooks billing", "money", _billing),
        Item("ar", "/ar", "Receivables", "chart", _admin),
        Item("followups", "/followups", "Payment follow-ups", "mail", _admin),
        Item("money", "/money", "Money summary", "tag", _perm("view_money")),
    )),
    Group("Operations", (
        Item("plan", "/routes/plan", "Route planner", "map", _perm("view_all_routes"),
             also=("/routes/",)),
        Item("alerts", "/water-alerts", "Water alerts", "drop", _admin,
             also=("/water-tests",)),
        Item("inventory", "/inventory", "Inventory", "box", _admin),
        Item("ops", "/operations", "Operations health", "pulse", _admin),
        Item("analytics", "/analytics", "Analytics", "chart", _admin),
    )),
    Group("Admin", (
        Item("settings", "/settings", "Admin", "gear", _admin,
             also=("/users", "/audit", "/testers")),
    )),
)

TECH = (
    Group("", (
        Item("today", "/", "Today", "today", also=("/visit/", "/today/")),
        Item("week", "/week", "Week", "week"),
        Item("customers", "/customers", "My customers", "users",
             also=("/properties/", "/pools/", "/search")),
        Item("account", "/account", "Account", "user"),
    )),
)


@dataclass
class NavItem:
    key: str
    href: str
    label: str
    icon: str
    on: bool = False
    badge: str = ""
    badge_cls: str = ""


@dataclass
class NavGroup:
    label: str
    items: list = field(default_factory=list)

    @property
    def on(self):
        return any(i.on for i in self.items)

    @property
    def href(self):
        return self.items[0].href

    @property
    def alert(self):
        return any(i.badge_cls == "neg" for i in self.items)


def _matches(item: Item, path: str) -> bool:
    if item.href == "/":
        return path == "/" or any(path.startswith(p) for p in item.also)
    return (path == item.href or path.startswith(item.href + "/")
            or any(path.startswith(p) for p in item.also))


def build(user, path: str, badges: dict | None = None) -> dict:
    """Everything base.html needs to draw the navigation for this request.

    `badges` maps an item key to (text, cls) -- cls "neg" marks something
    that needs a decision and also puts a dot on its group's tab."""
    badges = badges or {}
    is_tech = user is not None and user.role != "admin"
    source = TECH if is_tech else OWNER
    groups = []
    for g in source:
        items = [NavItem(i.key, i.href, i.label, i.icon, _matches(i, path),
                         *badges.get(i.key, ("", "")))
                 for i in g.items if i.show(user)]
        if items:
            groups.append(NavGroup(g.label, items))
    # Exactly one active item: the longest match wins (/routes/plan over /).
    flat = [i for g in groups for i in g.items]
    on = [i for i in flat if i.on]
    if len(on) > 1:
        best = max(on, key=lambda i: len(i.href))
        for i in on:
            i.on = i is best
    current = next((i for i in flat if i.on), None)
    cur_group = next((g for g in groups if g.on), None)
    single = len(groups) == 1
    top = flat if single else groups
    sub = cur_group.items if (cur_group and not single and len(cur_group.items) > 1) else []
    if is_tech:
        bnav = [("today", "/", "Today", "today"), ("week", "/week", "Week", "week"),
                ("customers", "/customers", "Customers", "users"),
                ("account", "/account", "Account", "user")]
    else:
        bnav = [("dash", "/dashboard", "Home", "dash"), ("today", "/", "Today", "today"),
                ("customers", "/customers", "Customers", "users"),
                ("invoices", "/invoices", "Money", "money")]
        visible = {i.key for i in flat}
        bnav = [b for b in bnav if b[0] in visible]
    return {"groups": groups, "top": top, "single": single, "sub": sub,
            "sub_label": f"{cur_group.label} pages" if sub else "",
            "current": current, "title": current.label if current else "",
            "bnav": bnav}
