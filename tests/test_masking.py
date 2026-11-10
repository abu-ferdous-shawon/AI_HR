from app.core.masking import Vault, is_confidential


def test_confidential_and_ordinary_field_names():
    for name in ("cell_number", "personal_email", "date_of_birth", "bank_ac_no", "ctc",
                 "passport_number", "current_address", "nid_number", "guardian_phone"):
        assert is_confidential(name), name
    for name in ("employee_name", "department", "designation", "date_of_joining",
                 "company", "status", "leave_type", "employee_id"):
        assert not is_confidential(name), name


def test_mask_hides_confidential_values_and_keeps_the_rest():
    out = Vault().mask({"result": {"details": {"cell_number": "01711000000",
                                               "department": "IT"}}})
    details = out["result"]["details"]
    assert details["department"] == "IT"
    assert details["cell_number"].startswith("[[MASK_")
    assert "01711000000" not in str(out)


def test_same_value_same_placeholder_different_values_differ():
    v = Vault()
    a = v.mask({"cell_number": "111", "emergency_phone_number": "111", "iban": "222"})
    assert a["cell_number"] == a["emergency_phone_number"] != a["iban"]


def test_unmask_restores_values():
    v = Vault()
    tok = v.mask({"cell_number": "01711000000"})["cell_number"]
    assert v.unmask(f"Your phone is {tok}.") == "Your phone is 01711000000."


def test_unmask_accepts_sloppy_placeholder_formats():
    v = Vault()
    v.mask({"cell_number": "111"})
    assert v.unmask("[[ mask_1 ]] and [[MASK_1]]") == "111 and 111"


def test_unknown_placeholders_are_never_shown_raw():
    assert Vault().unmask("x [[MASK_99]] y") == "x (not available) y"


def test_restored_text_is_not_scanned_again():
    v = Vault()
    tok = v.mask({"cell_number": "see [[MASK_2]]"})["cell_number"]
    assert v.unmask(tok) == "see [[MASK_2]]"


def test_empty_values_are_not_masked():
    data = {"cell_number": "", "bank_name": None, "iban": "  "}
    assert Vault().mask(data) == data


def test_numbers_are_masked_and_restored_as_text():
    v = Vault()
    tok = v.mask({"ctc": 50000.0})["ctc"]
    assert tok.startswith("[[MASK_") and v.unmask(tok) == "50000.0"