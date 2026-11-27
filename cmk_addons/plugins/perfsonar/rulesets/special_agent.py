"""Rule for the perfSONAR special agent (Setup > Agents > Other integrations)."""
from cmk.rulesets.v1 import Help, Title
from cmk.rulesets.v1.form_specs import DefaultValue, DictElement, Dictionary, TimeMagnitude, TimeSpan
from cmk.rulesets.v1.form_specs.validators import NumberInRange
from cmk.rulesets.v1.rule_specs import SpecialAgent, Topic

rule_spec_perfsonar = SpecialAgent(
    name="perfsonar",
    title=Title("perfSONAR toolkit (psetf)"),
    topic=Topic.NETWORKING,
    parameter_form=lambda: Dictionary(
        title=Title("perfSONAR toolkit"),
        help_text=Help("Fetches the perfSONAR toolkit summary and pScheduler statistics of the host."),
        elements={
            "timeout": DictElement(
                parameter_form=TimeSpan(
                    title=Title("Timeout per HTTP request"),
                    displayed_magnitudes=(TimeMagnitude.SECOND,),
                    prefill=DefaultValue(30.0),
                    custom_validate=[NumberInRange(min_value=1.0, max_value=300.0)],
                ),
            ),
        },
    ),
)
