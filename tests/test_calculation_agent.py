import pytest

from app.core import agent
from app.core.agent import run_agent
from app.core.llm.base import LLMProvider, LLMResponse, ToolCall
from app.core.masking import Vault
from app.core.security import UserContext

CTX = UserContext("u@x.com", "HR-EMP-00001")
SLIP = {"result": {"payslips": [{"gross_pay": 55000.0, "total_deduction": 5000.0,
                                 "net_pay": 50000.0}]}}


class Subtracts(LLMProvider):
    """Reads the payslip, subtracts deductions from gross pay, repeats the result."""
    def __init__(self):
        self.seen, self.step = [], 0

    def generate(self, system_prompt, messages, tools=None):
        self.seen.append(repr(messages))
        self.step += 1
        if self.step == 1:
            return LLMResponse(tool_calls=[ToolCall("get_my_payslips", {})])
        if self.step == 2:
            slip = messages[-1].tool_result["result"]["payslips"][0]
            return LLMResponse(tool_calls=[ToolCall(
                "calculate", {"expression": f"{slip['gross_pay']} - {slip['total_deduction']}"})])
        return LLMResponse(text=f"Difference: {messages[-1].tool_result['result']['value']}")


class PlainSubtraction(LLMProvider):
    def __init__(self):
        self.step = 0

    def generate(self, system_prompt, messages, tools=None):
        self.step += 1
        if self.step == 1:
            return LLMResponse(tool_calls=[ToolCall("calculate", {"expression": "10 - 5"})])
        return LLMResponse(text=f"Remaining: {messages[-1].tool_result['result']['value']}")


def test_arithmetic_on_confidential_numbers_never_shows_them_to_the_model(monkeypatch):
    real = agent.run_tool
    monkeypatch.setattr(agent, "run_tool", lambda name, args, ctx:
                        SLIP if name == "get_my_payslips" else real(name, args, ctx))
    llm = Subtracts()
    result = run_agent("what is gross pay minus deductions?", CTX, llm=llm)
    assert result.answer == "Difference: 50000"          # the person gets the real result
    seen = "".join(llm.seen) + repr(result.messages)
    for secret in ("55000", "50000"):                    # the model never saw any of them
        assert secret not in seen


def test_plain_numbers_are_calculated_and_stay_visible():
    result = run_agent("sick leave left after 5 days?", CTX, llm=PlainSubtraction())
    assert result.answer == "Remaining: 5"


def test_reveal_numbers_rejects_unknown_and_non_numeric_placeholders():
    vault = Vault()
    token = vault.mask({"cell_number": "abc@x.com"})["cell_number"]
    with pytest.raises(ValueError):
        vault.reveal_numbers(f"{token} + 1")
    with pytest.raises(ValueError):
        vault.reveal_numbers("[[MASK_99]] + 1")


def test_reveal_numbers_fills_real_values_and_reports_it():
    vault = Vault()
    token = vault.mask({"net_pay": "50,000.50"})["net_pay"]
    assert vault.reveal_numbers(f"{token} * 2") == ("(50000.50) * 2", True)
    assert vault.reveal_numbers("10 - 5") == ("10 - 5", False)


def test_calculated_secrets_are_restored_as_plain_numbers():
    vault = Vault()
    assert vault.unmask(vault.hide(45000.0)) == "45000"
    assert vault.unmask(vault.hide(12.5)) == "12.5"