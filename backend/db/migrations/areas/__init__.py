"""Per-area migration modules, merged with runner.MIGRATIONS and applied in
version order. Each module owns a reserved version range so areas can add
migrations independently without colliding."""

from backend.db.migrations.areas import infra, intelligence, relations, scam, sync

MODULES = [sync, scam, intelligence, relations, infra]
