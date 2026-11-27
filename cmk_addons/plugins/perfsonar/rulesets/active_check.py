"""Rule for the pscheduler diags active check (Setup > Services > HTTP, TCP, Email, ...)."""
from cmk.rulesets.v1 import Help, Title
from cmk.rulesets.v1.form_specs import (
    BooleanChoice,
    DefaultValue,
    DictElement,
    Dictionary,
    TimeMagnitude,
    TimeSpan,
)
from cmk.rulesets.v1.form_specs.validators import NumberInRange
from cmk.rulesets.v1.rule_specs import ActiveCheck, Topic

rule_spec_perfsonar_diags = ActiveCheck(
    name="perfsonar_diags",
    title=Title("perfSONAR pScheduler diagnostics (psetf)"),
    topic=Topic.NETWORKING,
    parameter_form=lambda: Dictionary(
        title=Title("pScheduler diagnostics"),
        help_text=Help("pscheduler troubleshoot over the pScheduler REST API: API, clock, services, "
                       "and an idle test that must run and be archived."),
        elements={
            "limit": DictElement(
                parameter_form=TimeSpan(
                    title=Title("Time limit for the whole check"),
                    help_text=Help("Must stay below the Nagios service check timeout."),
                    displayed_magnitudes=(TimeMagnitude.SECOND,),
                    prefill=DefaultValue(240.0),
                    custom_validate=[NumberInRange(min_value=10.0, max_value=3000.0)],
                ),
            ),
            "quick": DictElement(
                parameter_form=BooleanChoice(
                    title=Title("Quick: skip the idle test"),
                    prefill=DefaultValue(False),
                ),
            ),
        },
    ),
)
