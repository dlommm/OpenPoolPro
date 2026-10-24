"""The contract template: default content (today's contract, verbatim, with
dynamic values tokenized) + the loader that reads the admin-saved override."""
import json

from app import models

TEMPLATE_KEY = "contract_template"

DEFAULT_TEMPLATE = {
    "title": "Pool & Spa Service Agreement",
    "blocks": [
        {"kind": "text", "title": "", "body":
            'This Pool & Spa Service Agreement ("Agreement") is entered into between '
            '{{company_name}} ("Service Provider") and the undersigned Customer ("Client") '
            "for swimming pool and/or spa maintenance services at the property listed below, "
            "covering the {{pool_label}} pool."},
        {"kind": "parties", "visible": True},
        {"kind": "text", "title": "Scope of Weekly Pool Service", "body":
            "{{company_name}} will provide one (1) service visit per week on an assigned "
            "service day. Services may include: testing pool water chemistry, skimming and "
            "netting debris, vacuuming pool as needed, emptying skimmer and pump baskets, "
            "cleaning automatic pool cleaner, backwashing filter when necessary, brushing "
            "pool walls and tile when needed, inspecting filtration and circulation systems, "
            "adding necessary chemicals to maintain proper water balance. All labor "
            "associated with these services is billed at a rate of {{weekly_price}}/week, "
            "plus the cost of any chemicals, materials or additional services required."},
        {"kind": "text", "title": "Spa Service", "when": "includes_spa", "body":
            "Spa maintenance may include: vacuuming spa, cleaning strainer baskets and filter "
            "cartridges, testing water chemistry and adding necessary chemicals. All labor "
            "associated with these services is billed at a rate of {{spa_price}}/hour, plus "
            "the cost of any chemicals, materials or additional services required."},
        {"kind": "rates", "visible": True},
        {"kind": "text", "title": "Customer Requirements for Service", "body":
            "To maintain safe and effective service, the Client agrees to the following:\n"
            "- All chemicals must be purchased through {{company_name}}.\n"
            "- Pools must use an automatic chlorinator, or salt system.\n"
            "- Pool filtration must run approximately 12 hours per day via timer or automation.\n"
            "- If pool access requires a gate key, lock code, or security access, it must be provided.\n"
            "- Pets must be secured on the day of service.\n"
            "- The pool water level must be maintained at normal operating level.\n\n"
            "Failure to meet these requirements may prevent service completion and may still "
            "result in a service charge."},
        {"kind": "text", "title": "Photo & Marketing Release", "body":
            "Client grants {{company_name}} permission to photograph and/or record the pool "
            "and surrounding areas for the purpose of documenting services and for marketing, "
            "advertising, and promotional use (including social media and website). No "
            "personal identifying information (such as address or client name) will be "
            "disclosed without prior written consent."},
        {"kind": "text", "title": "Payment Terms", "body":
            "Payment for services, chemicals, and materials is due upon invoice unless "
            "otherwise agreed in writing. {{company_name}} reserves the right to suspend or "
            "terminate service for non-payment."},
        {"kind": "text", "title": "Equipment & Liability Disclaimer", "body":
            "Client acknowledges that {{company_name}} does not manufacture pool equipment "
            "and therefore cannot guarantee the performance of pumps, filters, heaters, "
            "plumbing, liners, or other equipment. {{company_name}} is not responsible for "
            "damage or service issues resulting from: equipment failure or aging components, "
            "improper installation by others, acts of nature (storm, flooding, lightning, "
            "debris, etc.), power outages or electrical issues, improper filtration operation "
            "or low water level. Any repairs required will be quoted separately."},
        {"kind": "text", "title": "Access & Missed Service", "body":
            "If service cannot be performed due to locked gates, pets, construction, unsafe "
            "conditions, or denied access, the visit may be skipped or billed as a service "
            "call. Service days may change due to weather, holidays, or operational scheduling."},
        {"kind": "text", "title": "Water Quality Disclaimer", "body":
            "While {{company_name}} will make every reasonable effort to maintain proper "
            "water quality, clarity, and algae prevention, these conditions cannot be "
            "guaranteed in the event of equipment failure, adverse weather conditions, "
            "improper filtration, or customer interference."},
        {"kind": "text", "title": "", "body":
            "By signing below, the Client agrees to all terms and conditions of this service "
            "agreement."},
        {"kind": "signature", "visible": True},
    ],
}


def get_template(session) -> dict:
    raw = models.get_setting(session, TEMPLATE_KEY, "")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("blocks"), list):
                return data
        except (ValueError, TypeError):
            pass
    return DEFAULT_TEMPLATE


# A sent contract with no signature after this many days lands in the
# Contracts hub's needs-attention digest. Admin-editable on the Contract tab.
STALE_DAYS_KEY = "contract_stale_days"
STALE_SENT_DAYS = 14
STALE_DAYS_MIN, STALE_DAYS_MAX = 1, 90


def parse_stale_days(raw) -> int | None:
    """A whole number of days in range, or None when it isn't one."""
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return days if STALE_DAYS_MIN <= days <= STALE_DAYS_MAX else None


def get_stale_days(session) -> int:
    """The stored threshold; a missing or out-of-range value falls back to the
    default rather than silently flagging every contract (0) or none."""
    days = parse_stale_days(models.get_setting(session, STALE_DAYS_KEY, ""))
    return STALE_SENT_DAYS if days is None else days
