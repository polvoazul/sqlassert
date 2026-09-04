from sqlassert import naming
from sqlassert.naming import NameGiver


def test_generated_identifiers_use_short_prefixes_and_one_shared_sequence():
    names = NameGiver()

    assert names.new(naming.RELATION, "users") == "rel_users_1"
    assert names.new(naming.RELATION, "customer") == "rel_customer_2"
    assert names.new(naming.COLUMN, "id") == "col_id_3"
    assert names.new(naming.PROPERTY, "candidate key") == "property_candidate_key_4"
    assert names.new(naming.JOIN) == "join_5"
