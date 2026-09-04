"""Superset inference is property logic, restricted to sets under analysis."""

import clingo
import pytest

from sqlassert.engine import rules


def _solve(facts: str) -> set[str]:
    control = clingo.Control(["0", "--warn=none"])
    control.add("base", [], rules() + "\n" + facts)
    control.ground([("base", [])])
    with control.solve(yield_=True) as models:
        results = [{str(atom) for atom in model.symbols(atoms=True)} for model in models]
    assert len(results) == 1
    return results[0]


_RELATION = """
ir__relation_expr__output_columns(r, 0, id).
ir__relation_expr__output_columns(r, 1, name).
ir__relation_expr__output_columns(r, 2, region).
"""


def test_a_set_of_interest_is_proved_unique_without_an_assertion_result():
    atoms = _solve(_RELATION + """
        pub__unique_set__columns(known, id).
        pub__column_set_of_interest(requested).
        pub__column_set_of_interest__columns(requested, id).
        pub__column_set_of_interest__columns(requested, name).
        pub__column_set_of_interest__columns(unrequested, id).
        pub__column_set_of_interest__columns(unrequested, region).
    """)

    assert {atom for atom in atoms if atom.startswith("pub__unique_set(")} == {
        "pub__unique_set(known)", "pub__unique_set(requested)"
    }
    assert "pub__unique_set__columns(requested,id)" in atoms
    assert "pub__unique_set__columns(requested,name)" in atoms
    assert not any(atom.startswith("pub__proved(") for atom in atoms)


@pytest.mark.parametrize("known", ["", "pub__unique_set__columns(known, region)."])
def test_sets_of_interest_cannot_prove_themselves_or_each_other(known):
    atoms = _solve(_RELATION + known + """
        pub__column_set_of_interest(first).
        pub__column_set_of_interest__columns(first, id).
        pub__column_set_of_interest(second).
        pub__column_set_of_interest__columns(second, id).
        pub__column_set_of_interest__columns(second, name).
    """)

    for column_set in ("first", "second"):
        assert f"pub__unique_set({column_set})" not in atoms
        assert not any(atom.startswith(f"pub__unique_set__columns({column_set},") for atom in atoms)


def test_a_subset_of_a_known_composite_unique_set_is_not_proved_unique():
    atoms = _solve(_RELATION + """
        pub__unique_set__columns(known, id).
        pub__unique_set__columns(known, region).
        pub__column_set_of_interest(requested).
        pub__column_set_of_interest__columns(requested, id).
        pub__column_set_of_interest__columns(requested, name).
    """)

    assert "pub__unique_set(requested)" not in atoms
    assert "pub__missing_unique_set_member(requested,known,region)" in atoms


def test_join_coverage_does_not_generalize_to_a_new_unique_set():
    atoms = _solve(_RELATION + """
        pub__unique_set__columns(known, id).
        ir__join(j).
        ir__join__kind(j, "inner").
        ir__join__right(j, r).
        ir__join__equalities(j, 0, eq).
        ir__equality__left(eq, ref).
        ir__equality__right(eq, value).
        ir__column_ref__column(ref, id).
        ir__constant(value).
    """)

    assert "pub__covers_unique_set(join_right_columns(j),known)" in atoms
    assert "pub__unique_join(established_unique_join(j))" in atoms
    assert "pub__unique_join__join(established_unique_join(j),j)" in atoms
    assert not any(atom.startswith("pub__column_set_of_interest(") for atom in atoms)
    assert {atom for atom in atoms if atom.startswith("pub__unique_set(")} == {"pub__unique_set(known)"}


def test_generalized_uniqueness_propagates_to_a_later_relation():
    atoms = _solve(_RELATION + """
        pub__unique_set__columns(known, id).
        pub__column_set_of_interest(requested).
        pub__column_set_of_interest__columns(requested, id).
        pub__column_set_of_interest__columns(requested, name).
        ir__project__input(project, r).
        ir__relation_expr__output_columns(project, 0, out_id).
        ir__relation_expr__output_columns(project, 1, out_name).
        ir__output_column__scalar_expr(out_id, ref_id).
        ir__output_column__scalar_expr(out_name, ref_name).
        ir__column_ref__column(ref_id, id).
        ir__column_ref__column(ref_name, name).
    """)

    assert "pub__unique_set__columns(derived_unique_set(project,requested),out_id)" in atoms
    assert "pub__unique_set__columns(derived_unique_set(project,requested),out_name)" in atoms
