from aleph_zero import lexical


def test_identifiers_split_on_case_underscores_and_digits():
    assert lexical.split_identifier("getUserName") == ["get", "user", "name"]
    assert lexical.split_identifier("HTTPServerError") == ["http", "server", "error"]
    assert lexical.split_identifier("MAX_RETRY_BACKOFF_EXPONENT") == ["max", "retry", "backoff", "exponent"]
    assert lexical.split_identifier("sha256sum") == ["sha", "256", "sum"]
    assert lexical.split_identifier("token") == []


def test_expand_keeps_the_whole_identifier_and_adds_its_parts():
    assert lexical.expand("def refresh_token(session):") == "def refresh_token refresh token session"


def test_query_terms_are_lower_cased_deduplicated_and_lose_stopwords():
    assert lexical.query_terms("where is the refreshToken refreshed") == ["refreshtoken", "refresh", "token", "refreshed"]
    assert lexical.query_terms("what is it") == ["what", "is", "it"]  # only stopwords: keep them
    assert lexical.query_terms("!!!") == []


def test_match_expression_quotes_each_term():
    assert lexical.match_expression(["refresh_token", "refresh"]) == '"refresh_token" OR "refresh"'


def test_the_view_filter_uses_the_form_that_runs_the_text_match_first():
    assert "+rowid IN (SELECT chunk_id FROM temp.view_chunks)" in lexical.SEARCH_SQL
