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


def test_words_in_any_script_are_kept_whole():
    assert lexical.expand("Die Übersicht") == "Die Übersicht"
    assert lexical.query_terms("設定") == ["設定"]
    assert lexical.query_terms("Привет, мир") == ["привет", "мир"]
    assert lexical.split_identifier("größe") == []


def test_identifiers_with_non_ascii_letters_split_on_case_too():
    assert lexical.split_identifier("getÜbersicht") == ["get", "übersicht"]
    assert lexical.split_identifier("ΑλφαΒήτα2") == ["αλφα", "βήτα", "2"]


def test_a_long_query_keeps_its_rarest_words(tmp_path):
    from aleph_zero.chunking import Chunk
    from aleph_zero.store import open_db

    conn = open_db(tmp_path / "index.db")
    for number in range(200):
        rare = "wombat_rotator" if number == 7 else ""
        text = f"common shared words appear in every chunk number_{number} {rare}"
        lexical.insert(conn, number + 1, Chunk("block", "", "", "", "", text, 1, 1))
        conn.execute("INSERT INTO temp.view_chunks(chunk_id) VALUES (?)", (number + 1,))
    filler = " ".join("zq" + chr(97 + i // 26) + chr(97 + i % 26) + "word" for i in range(40))  # never indexed
    terms = lexical.query_terms(f"common shared words appear in every chunk {filler} wombat_rotator")
    kept = lexical.select_terms(conn, terms)
    assert len(terms) > lexical.LONG_QUERY and len(kept) <= lexical.KEEP_TERMS
    assert kept[0] in ("wombat_rotator", "wombat", "rotator")       # the rarest first
    assert not any(term.startswith("zq") for term in kept)            # absent words cost nothing and say nothing
    short = lexical.query_terms("common shared wombat_rotator")
    assert lexical.select_terms(conn, short) == short                 # a short query is left alone
    long_query = f"common shared words appear in every chunk {filler} wombat_rotator"
    assert lexical.search(conn, long_query, 5)[0][0] == 8             # and the search finds the chunk with the rare word
