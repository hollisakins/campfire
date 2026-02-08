'use client';

import React from 'react';
import { Plus, X, GripVertical } from 'lucide-react';

// Types
export type FieldType = 'select' | 'multiselect' | 'number' | 'text';
export type Operator = 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | 'nin' | 'contains';
export type LogicOperator = 'AND' | 'OR';

export interface FieldDefinition {
  id: string;
  label: string;
  type: FieldType;
  options?: { value: string | number; label: string }[];
}

export interface Condition {
  id: string;
  field: string;
  operator: Operator;
  value: string | number | (string | number)[];
}

export interface ConditionGroup {
  id: string;
  logic: LogicOperator;
  conditions: (Condition | ConditionGroup)[];
}

// Operator labels
const OPERATORS: Record<FieldType, { value: Operator; label: string }[]> = {
  number: [
    { value: 'eq', label: '=' },
    { value: 'neq', label: '!=' },
    { value: 'gt', label: '>' },
    { value: 'gte', label: '>=' },
    { value: 'lt', label: '<' },
    { value: 'lte', label: '<=' },
  ],
  text: [
    { value: 'eq', label: '=' },
    { value: 'neq', label: '!=' },
    { value: 'contains', label: 'contains' },
  ],
  select: [
    { value: 'eq', label: '=' },
    { value: 'neq', label: '!=' },
  ],
  multiselect: [
    { value: 'in', label: 'any of' },
    { value: 'nin', label: 'none of' },
  ],
};

// Generate unique ID
const generateId = () => Math.random().toString(36).substr(2, 9);

// Check if item is a group
function isGroup(item: Condition | ConditionGroup): item is ConditionGroup {
  return 'logic' in item;
}

interface QueryBuilderProps {
  fields: FieldDefinition[];
  value: ConditionGroup;
  onChange: (value: ConditionGroup) => void;
  className?: string;
}

export const QueryBuilder: React.FC<QueryBuilderProps> = ({
  fields,
  value,
  onChange,
  className = '',
}) => {
  const updateGroup = (newGroup: ConditionGroup) => {
    onChange(newGroup);
  };

  const addCondition = (groupId: string) => {
    const newCondition: Condition = {
      id: generateId(),
      field: fields[0]?.id || '',
      operator: 'eq',
      value: '',
    };

    const addToGroup = (group: ConditionGroup): ConditionGroup => {
      if (group.id === groupId) {
        return { ...group, conditions: [...group.conditions, newCondition] };
      }
      return {
        ...group,
        conditions: group.conditions.map(item =>
          isGroup(item) ? addToGroup(item) : item
        ),
      };
    };

    updateGroup(addToGroup(value));
  };

  const addGroup = (parentGroupId: string) => {
    const newGroup: ConditionGroup = {
      id: generateId(),
      logic: 'AND',
      conditions: [],
    };

    const addToGroup = (group: ConditionGroup): ConditionGroup => {
      if (group.id === parentGroupId) {
        return { ...group, conditions: [...group.conditions, newGroup] };
      }
      return {
        ...group,
        conditions: group.conditions.map(item =>
          isGroup(item) ? addToGroup(item) : item
        ),
      };
    };

    updateGroup(addToGroup(value));
  };

  const removeItem = (itemId: string) => {
    const removeFromGroup = (group: ConditionGroup): ConditionGroup => {
      return {
        ...group,
        conditions: group.conditions
          .filter(item => item.id !== itemId)
          .map(item => isGroup(item) ? removeFromGroup(item) : item),
      };
    };

    updateGroup(removeFromGroup(value));
  };

  const updateCondition = (conditionId: string, updates: Partial<Condition>) => {
    const updateInGroup = (group: ConditionGroup): ConditionGroup => {
      return {
        ...group,
        conditions: group.conditions.map(item => {
          if (isGroup(item)) {
            return updateInGroup(item);
          }
          if (item.id === conditionId) {
            return { ...item, ...updates };
          }
          return item;
        }),
      };
    };

    updateGroup(updateInGroup(value));
  };

  const updateGroupLogic = (groupId: string, logic: LogicOperator) => {
    const updateInGroup = (group: ConditionGroup): ConditionGroup => {
      if (group.id === groupId) {
        return { ...group, logic };
      }
      return {
        ...group,
        conditions: group.conditions.map(item =>
          isGroup(item) ? updateInGroup(item) : item
        ),
      };
    };

    updateGroup(updateInGroup(value));
  };

  return (
    <div className={`${className}`}>
      <GroupComponent
        group={value}
        fields={fields}
        onAddCondition={addCondition}
        onAddGroup={addGroup}
        onRemoveItem={removeItem}
        onUpdateCondition={updateCondition}
        onUpdateGroupLogic={updateGroupLogic}
        isRoot={true}
      />
    </div>
  );
};

// Group component
interface GroupComponentProps {
  group: ConditionGroup;
  fields: FieldDefinition[];
  onAddCondition: (groupId: string) => void;
  onAddGroup: (groupId: string) => void;
  onRemoveItem: (itemId: string) => void;
  onUpdateCondition: (conditionId: string, updates: Partial<Condition>) => void;
  onUpdateGroupLogic: (groupId: string, logic: LogicOperator) => void;
  isRoot?: boolean;
}

const GroupComponent: React.FC<GroupComponentProps> = ({
  group,
  fields,
  onAddCondition,
  onAddGroup,
  onRemoveItem,
  onUpdateCondition,
  onUpdateGroupLogic,
  isRoot = false,
}) => {
  return (
    <div className={`
      rounded-lg border
      ${isRoot
        ? 'border-border dark:border-slate-700 bg-card dark:bg-slate-800'
        : 'border-dashed border-slate-300 dark:border-slate-600 bg-slate-50 dark:bg-slate-800/50'
      }
    `}>
      {/* Group header */}
      <div className="flex items-center gap-2 p-3 border-b border-border dark:border-slate-700">
        {!isRoot && (
          <GripVertical className="w-4 h-4 text-text-secondary dark:text-slate-500 cursor-grab" />
        )}

        {/* Logic toggle */}
        <div className="flex rounded-md overflow-hidden border border-border dark:border-slate-600">
          <button
            onClick={() => onUpdateGroupLogic(group.id, 'AND')}
            className={`px-3 py-1 text-xs font-medium transition-colors ${
              group.logic === 'AND'
                ? 'bg-primary text-white'
                : 'bg-card dark:bg-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-600'
            }`}
          >
            AND
          </button>
          <button
            onClick={() => onUpdateGroupLogic(group.id, 'OR')}
            className={`px-3 py-1 text-xs font-medium transition-colors ${
              group.logic === 'OR'
                ? 'bg-orange-500 text-white'
                : 'bg-card dark:bg-slate-700 text-text-secondary dark:text-slate-400 hover:bg-card-hover dark:hover:bg-slate-600'
            }`}
          >
            OR
          </button>
        </div>

        <span className="text-xs text-text-secondary dark:text-slate-500 ml-2">
          {group.logic === 'AND' ? 'All conditions must match' : 'Any condition can match'}
        </span>

        <div className="flex-1" />

        {/* Remove group button (not for root) */}
        {!isRoot && (
          <button
            onClick={() => onRemoveItem(group.id)}
            className="p-1 text-text-secondary dark:text-slate-400 hover:text-red-500 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Conditions */}
      <div className="p-3 space-y-2">
        {group.conditions.length === 0 ? (
          <div className="text-sm text-text-secondary dark:text-slate-500 text-center py-4">
            No conditions yet. Add a condition or group below.
          </div>
        ) : (
          group.conditions.map((item, index) => (
            <div key={item.id}>
              {/* Logic separator */}
              {index > 0 && (
                <div className="flex items-center gap-2 py-1">
                  <div className="flex-1 h-px bg-border dark:bg-slate-700" />
                  <span className={`text-xs font-medium px-2 ${
                    group.logic === 'AND'
                      ? 'text-primary'
                      : 'text-orange-500'
                  }`}>
                    {group.logic}
                  </span>
                  <div className="flex-1 h-px bg-border dark:bg-slate-700" />
                </div>
              )}

              {isGroup(item) ? (
                <GroupComponent
                  group={item}
                  fields={fields}
                  onAddCondition={onAddCondition}
                  onAddGroup={onAddGroup}
                  onRemoveItem={onRemoveItem}
                  onUpdateCondition={onUpdateCondition}
                  onUpdateGroupLogic={onUpdateGroupLogic}
                />
              ) : (
                <ConditionComponent
                  condition={item}
                  fields={fields}
                  onUpdate={(updates) => onUpdateCondition(item.id, updates)}
                  onRemove={() => onRemoveItem(item.id)}
                />
              )}
            </div>
          ))
        )}
      </div>

      {/* Add buttons */}
      <div className="flex gap-2 p-3 border-t border-border dark:border-slate-700">
        <button
          onClick={() => onAddCondition(group.id)}
          className="flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 hover:bg-card-hover dark:hover:bg-slate-700 rounded-md transition-colors"
        >
          <Plus className="w-4 h-4" />
          Add condition
        </button>
        <button
          onClick={() => onAddGroup(group.id)}
          className="flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 hover:bg-card-hover dark:hover:bg-slate-700 rounded-md transition-colors"
        >
          <Plus className="w-4 h-4" />
          Add group
        </button>
      </div>
    </div>
  );
};

// Condition component
interface ConditionComponentProps {
  condition: Condition;
  fields: FieldDefinition[];
  onUpdate: (updates: Partial<Condition>) => void;
  onRemove: () => void;
}

const ConditionComponent: React.FC<ConditionComponentProps> = ({
  condition,
  fields,
  onUpdate,
  onRemove,
}) => {
  const field = fields.find(f => f.id === condition.field);
  const operators = field ? OPERATORS[field.type] : OPERATORS.text;

  const handleFieldChange = (fieldId: string) => {
    const newField = fields.find(f => f.id === fieldId);
    const newOperators = newField ? OPERATORS[newField.type] : OPERATORS.text;
    onUpdate({
      field: fieldId,
      operator: newOperators[0]?.value || 'eq',
      value: newField?.type === 'multiselect' ? [] : '',
    });
  };

  return (
    <div className="flex items-center gap-2 p-2 bg-background dark:bg-slate-900 rounded-md border border-border dark:border-slate-700">
      <GripVertical className="w-4 h-4 text-text-secondary dark:text-slate-500 cursor-grab flex-shrink-0" />

      {/* Field selector */}
      <select
        value={condition.field}
        onChange={(e) => handleFieldChange(e.target.value)}
        className="px-2 py-1 text-sm bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded text-text-primary dark:text-slate-200"
      >
        {fields.map(f => (
          <option key={f.id} value={f.id}>{f.label}</option>
        ))}
      </select>

      {/* Operator selector */}
      <select
        value={condition.operator}
        onChange={(e) => onUpdate({ operator: e.target.value as Operator })}
        className="px-2 py-1 text-sm bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded text-text-primary dark:text-slate-200"
      >
        {operators.map(op => (
          <option key={op.value} value={op.value}>{op.label}</option>
        ))}
      </select>

      {/* Value input */}
      {field?.type === 'multiselect' && field.options ? (
        <select
          multiple
          value={Array.isArray(condition.value) ? condition.value.map(String) : []}
          onChange={(e) => {
            const selected = Array.from(e.target.selectedOptions, opt => {
              const numVal = Number(opt.value);
              return isNaN(numVal) ? opt.value : numVal;
            });
            onUpdate({ value: selected });
          }}
          className="flex-1 min-w-[150px] px-2 py-1 text-sm bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded text-text-primary dark:text-slate-200"
          size={Math.min(field.options.length, 4)}
        >
          {field.options.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      ) : field?.type === 'select' && field.options ? (
        <select
          value={String(condition.value)}
          onChange={(e) => {
            const numVal = Number(e.target.value);
            onUpdate({ value: isNaN(numVal) ? e.target.value : numVal });
          }}
          className="flex-1 px-2 py-1 text-sm bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded text-text-primary dark:text-slate-200"
        >
          <option value="">Select...</option>
          {field.options.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      ) : field?.type === 'number' ? (
        <input
          type="number"
          value={condition.value as number}
          onChange={(e) => onUpdate({ value: parseFloat(e.target.value) || 0 })}
          className="flex-1 px-2 py-1 text-sm bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded text-text-primary dark:text-slate-200"
          step="any"
        />
      ) : (
        <input
          type="text"
          value={condition.value as string}
          onChange={(e) => onUpdate({ value: e.target.value })}
          className="flex-1 px-2 py-1 text-sm bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded text-text-primary dark:text-slate-200"
          placeholder="Enter value..."
        />
      )}

      {/* Remove button */}
      <button
        onClick={onRemove}
        className="p-1 text-text-secondary dark:text-slate-400 hover:text-red-500 transition-colors flex-shrink-0"
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
};

// Export helper to create initial query
export function createEmptyQuery(): ConditionGroup {
  return {
    id: generateId(),
    logic: 'AND',
    conditions: [],
  };
}

// Helper to evaluate a query against data
export function evaluateQuery<T>(
  data: T[],
  query: ConditionGroup,
  getFieldValue: (item: T, field: string) => unknown
): T[] {
  const evaluateCondition = (item: T, condition: Condition): boolean => {
    const value = getFieldValue(item, condition.field);

    switch (condition.operator) {
      case 'eq':
        return value === condition.value;
      case 'neq':
        return value !== condition.value;
      case 'gt':
        return typeof value === 'number' && value > (condition.value as number);
      case 'gte':
        return typeof value === 'number' && value >= (condition.value as number);
      case 'lt':
        return typeof value === 'number' && value < (condition.value as number);
      case 'lte':
        return typeof value === 'number' && value <= (condition.value as number);
      case 'in': {
        const vals = condition.value as (string | number)[];
        if (Array.isArray(value)) {
          return vals.some(v => (value as (string | number)[]).includes(v));
        }
        return vals.includes(value as string | number);
      }
      case 'nin': {
        const vals = condition.value as (string | number)[];
        if (Array.isArray(value)) {
          return !vals.some(v => (value as (string | number)[]).includes(v));
        }
        return !vals.includes(value as string | number);
      }
      case 'contains':
        return typeof value === 'string' && value.toLowerCase().includes((condition.value as string).toLowerCase());
      default:
        return false;
    }
  };

  const evaluateGroup = (item: T, group: ConditionGroup): boolean => {
    if (group.conditions.length === 0) return true;

    const results = group.conditions.map(cond => {
      if (isGroup(cond)) {
        return evaluateGroup(item, cond);
      }
      return evaluateCondition(item, cond);
    });

    if (group.logic === 'AND') {
      return results.every(Boolean);
    }
    return results.some(Boolean);
  };

  return data.filter(item => evaluateGroup(item, query));
}
