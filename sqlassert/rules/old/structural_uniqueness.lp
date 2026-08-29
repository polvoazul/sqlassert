% Unique Sets established by relation-expression structure.

% Identifies a relation expression that groups rows into aggregate groups.
#defined aggregate_relation/1.
% Lists an aggregate relation's grouping output columns in their key order.
#defined aggregate_grouping_output/3.
% Identifies a relation expression that removes duplicate output rows.
#defined distinct_relation/1.
% Lists the output columns participating in duplicate removal.
#defined distinct_output/3.
% Identifies a relation expression that retains at most one row per partition.
#defined qualify_by_partition/1.
% Lists the output columns that identify a QUALIFY partition.
#defined qualify_partition_output/3.

% Declares grouping outputs as a Unique Set of their aggregate relation.
unique_set(aggregate_key(Relation), Relation) :- aggregate_relation(Relation).
% Lists each grouping output in the aggregate relation's Unique Set.
unique_set_column(aggregate_key(Relation), Position, Column) :-
    aggregate_grouping_output(Relation, Position, Column).

unique_set(distinct_key(Relation), Relation) :- distinct_relation(Relation).
unique_set_column(distinct_key(Relation), Position, Column) :-
    distinct_output(Relation, Position, Column).

unique_set(qualify_key(Relation), Relation) :- qualify_by_partition(Relation).
unique_set_column(qualify_key(Relation), Position, Column) :-
    qualify_partition_output(Relation, Position, Column).
