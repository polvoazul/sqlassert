% Unique Sets inherited through pass-through columns.

#defined relation_expression/1.        % Identifies every relation expression in the encoded program 
#defined relation_output_column/2.     % Lists the output columns produced by a relation expression.
#defined property_preserving_input/2.  % Connects a property-preserving relation expression to its upstream input.
#defined output_column_expression/2.   % Connects an output column to the scalar expression that produces it.
#defined direct_column_reference/2.    % Identifies an expression that directly references an upstream output column.
#defined arbitrary_group_value/2.      % Identifies an expression that selects an arbitrary value from an aggregate group.
#defined unique_set/2.                 % Declares a Unique Set belonging to a relation expression.
#defined unique_set_column/3.          % Lists the ordered output columns that belong to a Unique Set.
#defined non_null_column/2.            % Establishes that an output column cannot contain null.

% Maps a source column to a fresh target column that directly passes through its value.
passthrough_column(TargetRelation, SourceColumn, TargetColumn) :-
    property_preserving_input(TargetRelation, SourceRelation),
    relation_output_column(SourceRelation, SourceColumn),
    relation_output_column(TargetRelation, TargetColumn),
    output_column_expression(TargetColumn, OutputExpression),
    direct_column_reference(OutputExpression, SourceColumn).

% Preserves non-nullness through a pass-through column.
non_null_column(TargetRelation, TargetColumn) :-
    property_preserving_input(TargetRelation, SourceRelation),
    non_null_column(SourceRelation, SourceColumn),
    passthrough_column(TargetRelation, SourceColumn, TargetColumn).

% Selects an upstream Unique Set as a candidate for inheritance by the target relation.
inherited_unique_set_candidate(SourceUniqueSet, TargetRelation) :-
    property_preserving_input(TargetRelation, SourceRelation),
    unique_set(SourceUniqueSet, SourceRelation).

% Identifies a source Unique Set column with no pass-through target column.
unmapped_unique_set_column(SourceUniqueSet, TargetRelation, SourceColumn) :-
    inherited_unique_set_candidate(SourceUniqueSet, TargetRelation),
    unique_set_column(SourceUniqueSet, _, SourceColumn),
    not passthrough_column(TargetRelation, SourceColumn, _).

% Creates a derived Unique Set when every source Unique Set column passes through.
derived_unique_set(inherited_set(SourceUniqueSet, TargetRelation), SourceUniqueSet, TargetRelation) :-
    inherited_unique_set_candidate(SourceUniqueSet, TargetRelation),
    not unmapped_unique_set_column(SourceUniqueSet, TargetRelation, _).

unique_set(DerivedUniqueSet, TargetRelation) :-
    derived_unique_set(DerivedUniqueSet, SourceUniqueSet, TargetRelation).
unique_set_column(DerivedUniqueSet, ColumnPosition, TargetColumn) :-
    derived_unique_set(DerivedUniqueSet, SourceUniqueSet, TargetRelation),
    unique_set_column(SourceUniqueSet, ColumnPosition, SourceColumn),
    passthrough_column(TargetRelation, SourceColumn, TargetColumn).
