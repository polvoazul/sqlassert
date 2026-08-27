% Unique Sets inherited through pass-through columns.

#defined relation_expression/1.
#defined relation_output_column/2.
#defined property_preserving_input/2.
#defined output_column_expression/2.
#defined direct_column_reference/2.
#defined arbitrary_group_value/2.
#defined unique_set/2.
#defined unique_set_column/3.
#defined non_null_column/2.

passthrough_column(TargetRelation, SourceColumn, TargetColumn) :-
    property_preserving_input(TargetRelation, SourceRelation),
    relation_output_column(SourceRelation, SourceColumn),
    relation_output_column(TargetRelation, TargetColumn),
    output_column_expression(TargetColumn, OutputExpression),
    direct_column_reference(OutputExpression, SourceColumn).

non_null_column(TargetRelation, TargetColumn) :-
    property_preserving_input(TargetRelation, SourceRelation),
    non_null_column(SourceRelation, SourceColumn),
    passthrough_column(TargetRelation, SourceColumn, TargetColumn).

inherited_unique_set_candidate(SourceUniqueSet, TargetRelation) :-
    property_preserving_input(TargetRelation, SourceRelation),
    unique_set(SourceUniqueSet, SourceRelation).

unmapped_unique_set_column(SourceUniqueSet, TargetRelation, SourceColumn) :-
    inherited_unique_set_candidate(SourceUniqueSet, TargetRelation),
    unique_set_column(SourceUniqueSet, _, SourceColumn),
    not passthrough_column(TargetRelation, SourceColumn, _).

derived_unique_set(inherited_set(SourceUniqueSet, TargetRelation), SourceUniqueSet, TargetRelation) :-
    inherited_unique_set_candidate(SourceUniqueSet, TargetRelation),
    not unmapped_unique_set_column(SourceUniqueSet, TargetRelation, _).

unique_set(DerivedUniqueSet, TargetRelation) :-
    derived_unique_set(DerivedUniqueSet, SourceUniqueSet, TargetRelation).
unique_set_column(DerivedUniqueSet, ColumnPosition, TargetColumn) :-
    derived_unique_set(DerivedUniqueSet, SourceUniqueSet, TargetRelation),
    unique_set_column(SourceUniqueSet, ColumnPosition, SourceColumn),
    passthrough_column(TargetRelation, SourceColumn, TargetColumn).
