from evm_storage.layout import load_layout


def test_repairs_duplicated_hashmap_serialization():
    layout = load_layout(
        {
            "compiler": "vyper-0.2.16",
            "layout": {
                "balances": {
                    "type": "HashMap[address, uint256][address, uint256]",
                    "location": "storage",
                    "slot": 0,
                }
            },
        },
        language="vyper",
    )
    mapping = layout.types[layout.variables[0].type_id]
    assert mapping.label == "HashMap[address, uint256]"
    assert layout.types[mapping.key].label == "address"
    assert layout.types[mapping.value].label == "uint256"
    assert layout.metadata["hashmap_serialization_repaired"] is True


def test_vyper_legacy_dialect_from_worker():
    layout = load_layout(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": "0.2.12",
            "layout": {"values": {"type": "uint256[3]", "slot": 0, "n_slots": 1}},
            "extraction": {"method": "global-context", "storage_dialect": "legacy-hashed"},
        },
        language="vyper",
    )
    assert layout.hash_order == "slot-key"
    assert layout.storage_dialect == "vyper-hashed-composites"


def test_namespaced_layout_allows_variables_named_slot_and_type():
    layout = load_layout(
        {
            "compiler": "vyper-0.4.3",
            "layout": {
                "storage_layout": {
                    "module": {
                        "slot": {"type": "uint256", "slot": 1, "n_slots": 1},
                        "type": {"type": "address", "slot": 2, "n_slots": 1},
                    }
                }
            },
        },
        language="vyper",
    )
    assert [item.name for item in layout.variables] == ["module.slot", "module.type"]


def test_historical_bytes_span_changes_at_v030():
    old = load_layout(
        {"compiler": "vyper-0.2.16", "layout": {"data": {"type": "Bytes[65]", "slot": 0}}},
        language="vyper",
    )
    new = load_layout(
        {"compiler": "vyper-0.3.0", "layout": {"data": {"type": "Bytes[65]", "slot": 0}}},
        language="vyper",
    )
    assert old.types[old.variables[0].type_id].words == 5
    assert new.types[new.variables[0].type_id].words == 4


def test_legacy_worker_root_slot_does_not_collapse_bytes_span():
    layout = load_layout(
        {
            "schema": "evm-storage/vyper-worker/v1",
            "compiler_version": "0.2.12",
            "layout": {"data": {"type": "Bytes[65]", "slot": 0, "n_slots": 1}},
            "extraction": {"method": "global-context", "storage_dialect": "legacy-hashed"},
        },
        language="vyper",
    )
    assert layout.types[layout.variables[0].type_id].words == 4


def test_yanked_lock_alias_versions_are_flagged():
    layout = load_layout(
        {"layout": {"x": {"type": "uint256", "slot": 0, "n_slots": 1}}},
        language="vyper",
        compiler_version="0.2.13",
    )
    assert "yanked compiler" in layout.metadata["warnings"][0]
