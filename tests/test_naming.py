from sqlassert import naming
from sqlassert.naming import NameGiver


def test_generated_identifiers_use_short_prefixes_and_one_shared_sequence():
    names = NameGiver()

    assert names.new(naming.RELATION, "users") == "rel_users_1"
    assert names.new(naming.INSTANCE, "customer") == "rel_customer_2"
    assert names.new(naming.COLUMN, "id") == "col_id_3"
    assert names.new(naming.KEY, "users pk") == "key_users_pk_4"
    assert names.new(naming.JOIN) == "join_5"
