from __future__ import annotations

from collections import Counter, defaultdict
from fact_utils import load, save


def first_component(value):
    if not value:
        return None
    return str(value).split("|", 1)[0] or None


def build_family_aliases(hero_data):
    """
    Map abilityId/buttonId/linkId components/subAbilities to one canonical
    ability family. A subAbility is grouped under the first component of
    parent_link_id when that points to an ability; otherwise under its own id.
    """
    abilities = hero_data.get("abilities", [])
    known_ids = {a.get("id") for a in abilities if a.get("id")}

    alias_to_family = {}
    family_members = defaultdict(set)

    for a in abilities:
        aid = a.get("id")
        if not aid:
            continue

        family = aid

        parent_first = first_component(a.get("parent_link_id"))
        if parent_first and parent_first in known_ids:
            family = parent_first

        aliases = {
            aid,
            a.get("ability_id"),
            a.get("button_id"),
            a.get("link_id"),
            first_component(a.get("link_id")),
        }

        if a.get("parent_link_id"):
            aliases.add(a.get("parent_link_id"))
            aliases.add(first_component(a.get("parent_link_id")))

        for alias in aliases:
            if not alias:
                continue
            alias_to_family[str(alias)] = family
            family_members[family].add(str(alias))

    return alias_to_family, family_members


def resolve_candidate(alias_to_family, value):
    if not value:
        return None

    text = str(value)

    if text in alias_to_family:
        return alias_to_family[text]

    first = first_component(text)

    if first in alias_to_family:
        return alias_to_family[first]

    return None


def classify_talent(talent, families):
    """
    Status is intentionally semantic rather than 'ambiguous':
      LINKED              = one canonical family
      MULTI_ABILITY       = intentionally affects >1 family
      GLOBAL_MODIFIER     = no direct ability target and no hotkey/upgrade link
      GRANTS_ACTIVE       = talent itself grants a hotkey ability
      ORPHAN              = looks ability-specific but cannot be linked
    """
    has_hotkey = talent.get("has_hotkey")
    upgrades = talent.get("upgrades_ability_type")
    ability_type = str(talent.get("ability_type") or "").upper()

    ability_id = str(talent.get("ability_id") or "")

    if ability_id.upper() in {":PASSIVE:", "PASSIVE"}:
        return "PASSIVE_MODIFIER"

    if len(families) == 1:
        return "LINKED"

    if len(families) > 1:
        return "MULTI_ABILITY"

    if ability_type in {"ACTIVE", "ACTIVABLE"} or has_hotkey is True and upgrades is False:
        return "GRANTS_ACTIVE"

    if upgrades is False and not talent.get("ability_id"):
        return "GLOBAL_MODIFIER"

    return "ORPHAN"


def run_talent_parser(herodata_path, output_path, report_path):
    hd = load(herodata_path)

    heroes = {}
    total = 0
    status_counts = Counter()
    family_count_hist = Counter()
    review = []

    for hero, h in hd.get("heroes", {}).items():
        alias_to_family, family_members = build_family_aliases(h)

        rows = []

        for t in h.get("talents", []):
            total += 1

            evidence = []
            families = set()

            aid = t.get("ability_id")
            family = resolve_candidate(alias_to_family, aid)
            if aid:
                evidence.append({
                    "source": "ABILITY_ID",
                    "raw": aid,
                    "family": family,
                })
                if family:
                    families.add(family)

            button = t.get("button_id")
            family = resolve_candidate(alias_to_family, button)
            if button:
                evidence.append({
                    "source": "BUTTON_ID",
                    "raw": button,
                    "family": family,
                })
                if family:
                    families.add(family)

            link_id = t.get("link_id")
            family = resolve_candidate(alias_to_family, link_id)
            if link_id:
                evidence.append({
                    "source": "TALENT_LINK_ID",
                    "raw": link_id,
                    "family": family,
                })
                if family:
                    families.add(family)

            for lid in t.get("tooltip_ability_link_ids", []) or []:
                family = resolve_candidate(alias_to_family, lid)
                evidence.append({
                    "source": "TOOLTIP_LINK_ID",
                    "raw": lid,
                    "family": family,
                })
                if family:
                    families.add(family)

            families = sorted(families)
            status = classify_talent(t, families)

            status_counts[status] += 1
            family_count_hist[len(families)] += 1

            row = {
                **t,
                "status": status,
                "canonical_ability_families": families,
                "family_count": len(families),
                "link_evidence": evidence,
            }

            rows.append(row)

            if status == "ORPHAN":
                review.append({
                    "hero": hero,
                    "talent_id": t.get("talent_id"),
                    "name": t.get("name"),
                    "status": status,
                    "link_evidence": evidence,
                })

        heroes[hero] = rows

    data = {
        "metadata": {"builder": "talent_parser_v2_canonical_family"},
        "heroes": heroes,
    }

    report = {
        "summary": {
            "talents_total": total,
            "linked": status_counts["LINKED"],
            "multi_ability": status_counts["MULTI_ABILITY"],
            "global_modifier": status_counts["GLOBAL_MODIFIER"],
            "passive_modifier": status_counts["PASSIVE_MODIFIER"],
            "grants_active": status_counts["GRANTS_ACTIVE"],
            "orphan": status_counts["ORPHAN"],
        },
        "family_count_histogram": {
            str(k): v for k, v in sorted(family_count_hist.items())
        },
        "status_counts": dict(status_counts),
        "review": review,
    }

    save(output_path, data)
    save(report_path, report)
    return report
