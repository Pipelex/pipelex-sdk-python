"""Test data constants for the unit suite, grouped by what they stand for."""

from typing import ClassVar


class RefusedRunBodies:
    """Problem documents the dev plane answered when it refused to run a method, as they came off the wire.

    Copied byte for byte from `mthds-python`'s `tests/unit/test_data.py` (`RefusedRunBodies`), where
    they were captured on 2026-09-27 through `MthdsAPIClient` against `https://api-dev.pipelex.com`
    (pipelex-api v0.29.0 on pipelex 0.67.0): a bundle the runner refused at load with an itemized
    diagnostic, a run that failed at a pipe's combine step, and a run that failed on a model the deck
    does not serve. Keeping the same bytes in both clients' suites is what shows the two read a refusal
    alike.
    """

    UNKNOWN_MODEL_AT_LOAD: ClassVar[str] = (
        '{"type":"https://docs.pipelex.com/latest/errors/validate-bundle-error/","title":"Validate bundle","status":422,'
        "\"detail\":\"Pipe 'draft_pitch' (PipeLLM), field 'model': Model handle 'gpt-5.1' was not found in the model deck\\n\\n"
        'Did you mean: gpt-5.5, gpt-5.4, gpt-5.6-sol, gpt-5.4-pro, gpt-5.6-luna","instance":"/v1/execute",'
        '"request_id":"req_a3dd6900-7909-48d3-b551-0140e73ac7fc","error_category":"configuration","error_domain":"input",'
        '"retryable":false,"error_type":"ValidateBundleError","validation_errors":[{"category":"pipe_validation",'
        "\"message\":\"Pipe 'draft_pitch' (PipeLLM), field 'model': Model handle 'gpt-5.1' was not found in the model deck\\n\\n"
        'Did you mean: gpt-5.5, gpt-5.4, gpt-5.6-sol, gpt-5.4-pro, gpt-5.6-luna","error_type":"unknown_model",'
        '"pipe_code":"draft_pitch","domain_code":"sales_copy","field_path":"pipe.draft_pitch.model","field_name":"model",'
        '"model_reference":"gpt-5.1","model_type":"llm","suggestions":["gpt-5.5","gpt-5.4","gpt-5.6-sol","gpt-5.4-pro","gpt-5.6-luna"]}],'
        '"user_action":{"kind":"change_input","detail":"Edit the bundle as each validation error says: apply its suggested fix '
        'where it has one, after confirming an unsafe one"}}'
    )
    UNKNOWN_MODEL_DETAIL: ClassVar[str] = (
        "Pipe 'draft_pitch' (PipeLLM), field 'model': Model handle 'gpt-5.1' was not found in the model deck\n\n"
        "Did you mean: gpt-5.5, gpt-5.4, gpt-5.6-sol, gpt-5.4-pro, gpt-5.6-luna"
    )
    UNKNOWN_MODEL_NEXT_STEP: ClassVar[str] = (
        "Edit the bundle as each validation error says: apply its suggested fix where it has one, after confirming an unsafe one"
    )

    COMBINE_FAILURE_AT_RUN: ClassVar[str] = (
        '{"type":"https://docs.pipelex.com/latest/errors/stuff-factory-error/","title":"Stuff factory","status":422,'
        "\"detail\":\"Pipe 'analyze_topics' failed (review_topics → analyze_topics): PipeParallel 'analyze_topics' cannot "
        "combine its branch results into its output 'TopicReview'. Branch 'draft_ideas' gives result 'ideas' as a list, "
        "'Idea[]', but field 'ideas' of 'TopicReview' holds a single item. Declare the field as a list in the structure of "
        "'TopicReview', with type 'list', item_type 'concept' and item_concept_ref 'Idea', or make branch 'draft_ideas' output "
        'a single \'Idea\'.","instance":"/v1/execute","request_id":"req_d4212542-63a6-4ddb-87c9-4b968785c8c5",'
        '"error_domain":"input","error_type":"StuffFactoryError","user_action":{"kind":"change_input","detail":"Branch '
        "'draft_ideas' gives result 'ideas' as a list, 'Idea[]', but field 'ideas' of 'TopicReview' holds a single item. "
        "Declare the field as a list in the structure of 'TopicReview', with type 'list', item_type 'concept' and "
        "item_concept_ref 'Idea', or make branch 'draft_ideas' output a single 'Idea'.\"}}"
    )
    COMBINE_FAILURE_NEXT_STEP: ClassVar[str] = (
        "Branch 'draft_ideas' gives result 'ideas' as a list, 'Idea[]', but field 'ideas' of 'TopicReview' holds a single item. "
        "Declare the field as a list in the structure of 'TopicReview', with type 'list', item_type 'concept' and "
        "item_concept_ref 'Idea', or make branch 'draft_ideas' output a single 'Idea'."
    )

    UNSERVED_MODEL_AT_RUN: ClassVar[str] = (
        '{"type":"https://docs.pipelex.com/latest/errors/model-not-found-error/","title":"Model not found","status":422,'
        "\"detail\":\"Pipe 'condense_article' failed (digest_article → condense_article): Model handle 'gpt-5.1' was not "
        'found in the model deck.","instance":"/v1/execute","request_id":"req_b7d2c66f-b2d5-4dc7-bd4f-6cf98abfbdc0",'
        '"error_category":"configuration","error_domain":"input","retryable":false,"error_type":"ModelNotFoundError",'
        '"user_action":{"kind":"change_model","detail":"Change the model \'gpt-5.1\' to an LLM the model deck serves."}}'
    )
    UNSERVED_MODEL_DETAIL: ClassVar[str] = (
        "Pipe 'condense_article' failed (digest_article → condense_article): Model handle 'gpt-5.1' was not found in the model deck."
    )
    UNSERVED_MODEL_NEXT_STEP: ClassVar[str] = "Change the model 'gpt-5.1' to an LLM the model deck serves."
