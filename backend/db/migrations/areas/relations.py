"""Contacts/orgs/tags/workspaces/boards migrations. Versions 90-109 are reserved for this area.

Same rules as runner.MIGRATIONS: append-only (version, description, [sql]).

Note: the runner skips statements failing with "no such column", so every
statement here only references columns that exist in both upgraded and fresh
(create_all) schemas. tests/test_relations_migrations.py replays them.
"""

# A tag is "protected" when it carries user meaning outside a single board
# column: workspace membership (include/exclude rule) or a board's stream scope.
_PROTECTED_TAG_IDS = (
    "(SELECT tag_id FROM workspace_tags "
    "UNION SELECT scope_tag_id FROM workspace_boards WHERE scope_tag_id IS NOT NULL)"
)

# Members of workspace `ws_expr` = chats carrying one of its include tags.
def _members_of(ws_expr: str) -> str:
    return (
        "(SELECT ct_m.chat_id FROM chat_tags ct_m JOIN workspace_tags wt_m ON wt_m.tag_id = ct_m.tag_id "
        f"WHERE wt_m.workspace_id = {ws_expr} AND wt_m.is_exclusion = 0)"
    )


_STAGE_SPLIT = [
    "DROP TABLE IF EXISTS temp._stage_split",
    # Every column whose backing tag is not exclusively its own stage tag:
    # a non-stage tag (resolved by global name), a protected tag, or a stage
    # tag already used by a lower-id column (that column keeps it).
    "CREATE TEMP TABLE _stage_split AS "
    "SELECT wc.id AS col_id, wc.workspace_id AS workspace_id, wc.board_id AS board_id, "
    "  wc.tag_id AS old_tag_id, COALESCE(wc.label, t.name) AS label, "
    "  COALESCE(wc.color, t.color, 'slate') AS color, "
    # Cards are copied only when placement is unambiguous: if the tag is this
    # workspace's own include tag or one of its stream scopes, every member
    # 'sat' in the column and no staging decision can be recovered.
    "  CASE WHEN EXISTS (SELECT 1 FROM workspace_tags wt WHERE wt.tag_id = wc.tag_id "
    "                    AND wt.workspace_id = wc.workspace_id) "
    "         OR EXISTS (SELECT 1 FROM workspace_boards b WHERE b.scope_tag_id = wc.tag_id "
    "                    AND b.workspace_id = wc.workspace_id) "
    "       THEN 0 ELSE 1 END AS copy_cards, "
    "  CAST(NULL AS INTEGER) AS new_tag_id "
    "FROM workspace_columns wc JOIN tags t ON t.id = wc.tag_id "
    "WHERE COALESCE(t.category, '') != 'stage' "
    f"   OR wc.tag_id IN {_PROTECTED_TAG_IDS} "
    "   OR EXISTS (SELECT 1 FROM workspace_columns c2 WHERE c2.tag_id = wc.tag_id AND c2.id < wc.id)",
    "INSERT INTO tags (name, color, category, is_default, created_at) "
    "SELECT '__stage_split_' || col_id, color, 'stage', 0, CURRENT_TIMESTAMP FROM _stage_split",
    "UPDATE _stage_split SET new_tag_id = "
    "(SELECT id FROM tags WHERE name = '__stage_split_' || _stage_split.col_id)",
    # Preserve placement: chats that showed in the column (carry the old tag
    # and are members of the column's workspace), unless the chat is already
    # staged in another, unsplit column of the same board.
    "INSERT OR IGNORE INTO chat_tags (chat_id, tag_id) "
    "SELECT ct.chat_id, s.new_tag_id FROM _stage_split s JOIN chat_tags ct ON ct.tag_id = s.old_tag_id "
    "WHERE s.copy_cards = 1 "
    f"  AND ct.chat_id IN {_members_of('s.workspace_id')} "
    "  AND NOT EXISTS (SELECT 1 FROM chat_tags ct3 JOIN workspace_columns c3 ON c3.tag_id = ct3.tag_id "
    "                  WHERE ct3.chat_id = ct.chat_id AND c3.board_id = s.board_id AND c3.id != s.col_id "
    "                    AND c3.id NOT IN (SELECT col_id FROM _stage_split))",
    "UPDATE workspace_columns SET tag_id = "
    "(SELECT new_tag_id FROM _stage_split s WHERE s.col_id = workspace_columns.id) "
    "WHERE id IN (SELECT col_id FROM _stage_split)",
    # Readable internal names: "<label> · stage <id>" (unique by id; falls
    # back to the placeholder only if a user tag already holds that name).
    "UPDATE tags SET name = (SELECT trim(s.label) FROM _stage_split s WHERE s.new_tag_id = tags.id) "
    "  || ' · stage ' || tags.id "
    "WHERE tags.id IN (SELECT new_tag_id FROM _stage_split) "
    "  AND NOT EXISTS (SELECT 1 FROM tags t2 WHERE t2.id != tags.id AND lower(trim(t2.name)) = "
    "      lower(trim((SELECT trim(s.label) FROM _stage_split s WHERE s.new_tag_id = tags.id) "
    "      || ' · stage ' || tags.id)))",
    "DROP TABLE temp._stage_split",
    # Orphan stage tags (left behind by delete_workspace / deleted columns)
    # would instantly refill a new column of the same name — remove them.
    "DELETE FROM chat_tags WHERE tag_id IN (SELECT id FROM tags WHERE category = 'stage' "
    "  AND id NOT IN (SELECT tag_id FROM workspace_columns) "
    f"  AND id NOT IN {_PROTECTED_TAG_IDS})",
    "DELETE FROM tags WHERE category = 'stage' "
    "  AND id NOT IN (SELECT tag_id FROM workspace_columns) "
    f"  AND id NOT IN {_PROTECTED_TAG_IDS}",
]


_TAG_DEDUPE = [
    # Stage tags are internal; never merge one into a user tag. Rename any
    # that collides (case/whitespace-insensitively) out of the way first.
    "UPDATE tags SET name = trim(name) || ' · stage ' || id "
    "WHERE category = 'stage' AND EXISTS (SELECT 1 FROM tags t2 WHERE t2.id != tags.id "
    "  AND lower(trim(t2.name)) = lower(trim(tags.name)))",
    "DROP TABLE IF EXISTS temp._tag_merge",
    # Keeper per case-insensitive name: default tags first, then the oldest.
    "CREATE TEMP TABLE _tag_merge AS "
    "SELECT t.id AS old_id, (SELECT k.id FROM tags k WHERE lower(trim(k.name)) = lower(trim(t.name)) "
    "  ORDER BY k.is_default DESC, k.id LIMIT 1) AS keep_id FROM tags t",
    "DELETE FROM _tag_merge WHERE old_id = keep_id",
    "INSERT OR IGNORE INTO chat_tags (chat_id, tag_id) "
    "SELECT ct.chat_id, m.keep_id FROM chat_tags ct JOIN _tag_merge m ON m.old_id = ct.tag_id",
    "DELETE FROM chat_tags WHERE tag_id IN (SELECT old_id FROM _tag_merge)",
    "INSERT OR IGNORE INTO workspace_tags (workspace_id, tag_id, is_exclusion) "
    "SELECT wt.workspace_id, m.keep_id, wt.is_exclusion FROM workspace_tags wt "
    "JOIN _tag_merge m ON m.old_id = wt.tag_id",
    "DELETE FROM workspace_tags WHERE tag_id IN (SELECT old_id FROM _tag_merge)",
    # Unreachable after migration 90 (column tags are exclusive stage tags),
    # kept so the merge is complete on any input.
    "UPDATE OR IGNORE workspace_columns SET tag_id = "
    "(SELECT keep_id FROM _tag_merge WHERE old_id = workspace_columns.tag_id) "
    "WHERE tag_id IN (SELECT old_id FROM _tag_merge)",
    "UPDATE workspace_boards SET scope_tag_id = "
    "(SELECT keep_id FROM _tag_merge WHERE old_id = workspace_boards.scope_tag_id) "
    "WHERE scope_tag_id IN (SELECT old_id FROM _tag_merge)",
    "DELETE FROM tags WHERE id IN (SELECT old_id FROM _tag_merge) "
    "  AND id NOT IN (SELECT tag_id FROM workspace_columns)",
    "DROP TABLE temp._tag_merge",
    "UPDATE tags SET name = trim(name) WHERE name != trim(name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_name_ci ON tags (lower(trim(name)))",
]


_ORG_FK = [
    # 1. Merge case/whitespace duplicate organizations (keep the one with
    #    context, then the oldest).
    "DROP TABLE IF EXISTS temp._org_merge",
    "CREATE TEMP TABLE _org_merge AS "
    "SELECT o.id AS old_id, (SELECT k.id FROM organizations k WHERE lower(trim(k.name)) = lower(trim(o.name)) "
    "  ORDER BY (k.context IS NULL OR trim(k.context) = ''), k.id LIMIT 1) AS keep_id FROM organizations o",
    "DELETE FROM _org_merge WHERE old_id = keep_id",
    "DELETE FROM organizations WHERE id IN (SELECT old_id FROM _org_merge)",
    "DROP TABLE temp._org_merge",
    "UPDATE organizations SET name = trim(name) WHERE name != trim(name)",
    # 2. Every distinct position org string gets an organization row.
    "INSERT INTO organizations (name, created_at) "
    "SELECT min(trim(cp.organization)), min(cp.created_at) FROM contact_positions cp "
    "WHERE trim(cp.organization) != '' AND NOT EXISTS (SELECT 1 FROM organizations o "
    "  WHERE lower(o.name) = lower(trim(cp.organization))) "
    "GROUP BY lower(trim(cp.organization))",
    # 3. Rebuild contact_positions keyed by organization_id. Case variants of
    #    one org for the same person collapse to one row: active first, then
    #    the one with a title, then the oldest.
    "DROP TABLE IF EXISTS contact_positions_rebuild",
    "CREATE TABLE contact_positions_rebuild ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  telegram_id BIGINT NOT NULL,"
    "  organization_id INTEGER REFERENCES organizations (id),"
    "  organization VARCHAR NOT NULL,"
    "  title VARCHAR,"
    "  is_active INTEGER NOT NULL DEFAULT 1,"
    "  ended_at TEXT,"
    "  created_at DATETIME NOT NULL DEFAULT (datetime('now')),"
    "  CONSTRAINT uq_telegram_org UNIQUE (telegram_id, organization_id)"
    ")",
    "INSERT INTO contact_positions_rebuild "
    "  (id, telegram_id, organization_id, organization, title, is_active, ended_at, created_at) "
    "SELECT id, telegram_id, org_id, COALESCE(org_name, trim(organization)), title, is_active, ended_at, created_at "
    "FROM (SELECT cp.id, cp.telegram_id, cp.organization, cp.title, cp.is_active, cp.ended_at, cp.created_at, "
    "        o.id AS org_id, o.name AS org_name, "
    "        ROW_NUMBER() OVER (PARTITION BY cp.telegram_id, COALESCE(o.id, -cp.id) "
    "          ORDER BY cp.is_active DESC, (cp.title IS NULL OR cp.title = ''), cp.id) AS rn "
    "      FROM contact_positions cp "
    "      LEFT JOIN organizations o ON lower(o.name) = lower(trim(cp.organization))) "
    "WHERE rn = 1",
    "DROP TABLE contact_positions",
    "ALTER TABLE contact_positions_rebuild RENAME TO contact_positions",
    "CREATE INDEX IF NOT EXISTS ix_contact_positions_telegram_id ON contact_positions (telegram_id)",
    "CREATE INDEX IF NOT EXISTS ix_contact_positions_organization_id ON contact_positions (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_organizations_name_ci ON organizations (lower(trim(name)))",
]


MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        90,
        "Kanban: give every column its own stage tag; drop orphan stage tags",
        _STAGE_SPLIT,
    ),
    (
        91,
        "Tags: merge case/whitespace duplicates; unique index on lower(trim(name))",
        _TAG_DEDUPE,
    ),
    (
        92,
        "Organizations: merge duplicates, backfill from positions, key positions by organization_id",
        _ORG_FK,
    ),
]
