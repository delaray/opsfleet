"""First live vertical slice: authorized item counts with exact output grounding."""

import json

from .graph import AnalysisServices, PolicyDenied, build_graph
from .trusted_bq import TrustedBigQueryExecutor

COUNT_QUESTION = "How many order items can I access?"
COUNT_SQL = "SELECT COUNT(*) AS `item_count` FROM retail_items"


def build_count_graph(executor: TrustedBigQueryExecutor, *, model: str, llm=None):
    def authorize(question, user_id):
        executor.retrieve_context("", user_id)  # Verify grants before inference.
        if question.strip().casefold().rstrip("?.") != COUNT_QUESTION.casefold().rstrip("?"):
            raise PolicyDenied("This initial flow supports only the item-count question")
        return COUNT_QUESTION

    def validate(sql, user_id):
        canonical = executor.validate_sql(sql, user_id)
        if canonical != COUNT_SQL:
            raise PolicyDenied("The generated SQL does not match the approved count request")
        return canonical

    def context(question, user_id):
        return executor.retrieve_context(question, user_id) + (
            "\nFor this request count all entitled order items with no filters, grouping, "
            "ordering or limit. Use exactly: " + COUNT_SQL)

    def execute(sql, user_id):
        # Recheck the request-specific contract as well as executor access policies.
        return executor.execute_sql(validate(sql, user_id), user_id)

    def validate_output(answer, evidence, user_id):
        executor.retrieve_context("", user_id)  # Reject access revoked during inference.
        try:
            payload = json.loads(evidence)
            rows = payload["rows"]
            if payload.get("cohort_suppression_applied") is not True:
                raise ValueError()
            if rows == []:
                expected = None
            elif (len(rows) == 1 and set(rows[0]) == {"item_count"}
                  and type(rows[0]["item_count"]) is int and rows[0]["item_count"] >= 0):
                expected = rows[0]["item_count"]
            else:
                raise ValueError()
            draft = json.loads(answer)
            if (not isinstance(draft, dict) or set(draft) != {"item_count"}
                    or type(draft["item_count"]) is not type(expected)
                    or draft["item_count"] != expected):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise PolicyDenied("The model answer is not grounded in the query result") from None
        if expected is None:
            return "No publishable aggregate was returned; activity may be below the minimum customer cohort."
        return f"Your authorized products have {expected} order items. The minimum customer cohort check passed."

    services = AnalysisServices(authorize, context, validate, execute, validate_output)
    # Authorization has already matched the sole supported analysis question.
    return build_graph(services, model=model, llm=llm, classify_request=lambda _: 'analysis', answer_instructions=(
        'Return only a JSON object {"item_count": integer}, without markdown or any other fields. '
        'Copy the item_count exactly from the evidence. If rows is empty, return {"item_count": null}.'
    ))
