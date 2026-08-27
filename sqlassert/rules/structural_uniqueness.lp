% Unique Sets established by relation-expression structure.

#defined aggregate_relation/1.
#defined aggregate_grouping_output/3.
#defined distinct_relation/1.
#defined distinct_output/3.
#defined qualify_by_partition/1.
#defined qualify_partition_output/3.

unique_set(aggregate_key(Relation), Relation) :- aggregate_relation(Relation).
unique_set_column(aggregate_key(Relation), Position, Column) :-
    aggregate_grouping_output(Relation, Position, Column).

unique_set(distinct_key(Relation), Relation) :- distinct_relation(Relation).
unique_set_column(distinct_key(Relation), Position, Column) :-
    distinct_output(Relation, Position, Column).

unique_set(qualify_key(Relation), Relation) :- qualify_by_partition(Relation).
unique_set_column(qualify_key(Relation), Position, Column) :-
    qualify_partition_output(Relation, Position, Column).
