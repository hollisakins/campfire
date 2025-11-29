/**
 * Base Filter System
 * Unified filtering functionality based on NIRCam's mature patterns
 */

class BaseFilters {
    constructor(config = {}) {
        this.config = {
            searchable: config.searchable !== false,
            collapsible: config.collapsible !== false,
            showCounts: config.showCounts !== false,
            debounceMs: config.debounceMs || 100,
            ...config
        };

        this.filterState = {};
        this.callbacks = {
            onFilterChange: config.onFilterChange || (() => {}),
            onClear: config.onClear || (() => {})
        };

        this.debouncedApply = this.debounce(this.applyFilters.bind(this), this.config.debounceMs);
    }

    /**
     * Initialize filter system
     * @param {Object} filterDefinitions - Define available filters
     * @param {Array} data - Initial data to extract filter options from
     */
    initialize(filterDefinitions, data = []) {
        this.filterDefinitions = filterDefinitions;
        this.initializeFilterState();
        this.populateFilterOptions(data);
        this.setupEventListeners();
    }

    /**
     * Initialize filter state based on definitions
     */
    initializeFilterState() {
        Object.keys(this.filterDefinitions).forEach(filterKey => {
            const definition = this.filterDefinitions[filterKey];
            if (definition.type === 'multi-select' || definition.type === 'checkbox-group') {
                this.filterState[filterKey] = [];
            } else if (definition.type === 'range') {
                this.filterState[`${filterKey}Min`] = '';
                this.filterState[`${filterKey}Max`] = '';
            } else {
                this.filterState[filterKey] = '';
            }
        });
    }

    /**
     * Populate filter options from data
     * @param {Array} data - Data array to extract unique values from
     */
    populateFilterOptions(data) {
        console.log('BaseFilters.populateFilterOptions called with', data.length, 'items');
        Object.entries(this.filterDefinitions).forEach(([filterKey, definition]) => {
            console.log(`Processing filter: ${filterKey}`, definition);
            if (definition.type === 'checkbox-group') {
                const container = document.getElementById(definition.containerId);
                if (!container) {
                    console.warn(`Container not found for ${filterKey}: ${definition.containerId}`);
                    return;
                }

                let options = [];
                if (definition.dataField) {
                    // Extract unique values from data
                    const fieldValues = data.map(item => item[definition.dataField]).filter(val => val);
                    // Handle array fields by flattening them
                    const flatValues = fieldValues.flat();
                    options = [...new Set(flatValues)];
                    console.log(`${filterKey}: extracted ${options.length} options from dataField '${definition.dataField}'`);
                } else if (definition.staticOptions) {
                    // Use predefined options
                    options = definition.staticOptions;
                    console.log(`${filterKey}: using ${options.length} static options`);
                } else {
                    console.log(`${filterKey}: no dataField or staticOptions defined`);
                }

                if (options.length > 0) {
                    this.populateCheckboxGroup(container, options, filterKey);
                } else {
                    console.warn(`No options found for ${filterKey}`);
                }
            }
        });
    }

    /**
     * Create checkbox group in container
     * @param {HTMLElement} container - Container element
     * @param {Array} options - Array of option values
     * @param {string} filterKey - Filter key for state management
     */
    populateCheckboxGroup(container, options, filterKey) {
        container.innerHTML = '';
        const definition = this.filterDefinitions[filterKey];
        
        options.forEach(option => {
            // Use custom label if available, otherwise use the option value
            const displayText = definition.optionLabels?.[option] || option;
            const checkboxItem = this.createCheckboxItem(option, displayText, filterKey);
            container.appendChild(checkboxItem);
        });
    }

    /**
     * Create individual checkbox item
     * @param {string} value - Checkbox value
     * @param {string} displayText - Display text for label
     * @param {string} filterKey - Filter key for identification
     * @returns {HTMLElement} Checkbox item element
     */
    createCheckboxItem(value, displayText, filterKey) {
        const item = document.createElement('div');
        item.className = 'checkbox-item';
        
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = `${filterKey}-${value}`;
        checkbox.value = value;
        
        const label = document.createElement('label');
        label.htmlFor = checkbox.id;
        label.textContent = displayText;
        
        // Add event listener
        checkbox.addEventListener('change', () => {
            this.updateFilterFromCheckbox(filterKey, value, checkbox.checked);
        });
        
        item.appendChild(checkbox);
        item.appendChild(label);
        
        return item;
    }

    /**
     * Update filter state from checkbox change
     * @param {string} filterKey - Filter key
     * @param {string} value - Checkbox value
     * @param {boolean} checked - Checkbox state
     */
    updateFilterFromCheckbox(filterKey, value, checked) {
        if (checked) {
            if (!this.filterState[filterKey].includes(value)) {
                this.filterState[filterKey].push(value);
            }
        } else {
            const index = this.filterState[filterKey].indexOf(value);
            if (index > -1) {
                this.filterState[filterKey].splice(index, 1);
            }
        }

        this.updateFilterCount(filterKey);
        this.debouncedApply();
    }

    /**
     * Update filter count badge
     * @param {string} filterKey - Filter key
     */
    updateFilterCount(filterKey) {
        if (!this.config.showCounts) return;

        const countElement = document.getElementById(`${filterKey}-count`);
        if (countElement) {
            const selectedCount = this.filterState[filterKey]?.length || 0;
            countElement.textContent = selectedCount > 0 ? selectedCount : '';
        }
    }

    /**
     * Setup event listeners for filter controls
     */
    setupEventListeners() {
        // Search input
        if (this.config.searchable) {
            const searchInput = document.getElementById('searchInput');
            if (searchInput) {
                searchInput.addEventListener('input', (e) => {
                    this.filterState.search = e.target.value;
                    this.debouncedApply();
                });
            }
        }

        // Filter toggles
        if (this.config.collapsible) {
            this.setupFilterToggles();
        }

        // Clear filters button
        const clearButton = document.getElementById('clear-filters');
        if (clearButton) {
            clearButton.addEventListener('click', () => {
                this.clearAllFilters();
            });
        }

        // Range inputs
        this.setupRangeInputs();
    }

    /**
     * Setup filter toggle functionality (NIRCam style)
     */
    setupFilterToggles() {
        const filterToggles = document.querySelectorAll('.filter-toggle');
        
        filterToggles.forEach(toggle => {
            toggle.addEventListener('click', (e) => {
                e.preventDefault();
                const targetId = toggle.getAttribute('data-target');
                const targetContainer = document.getElementById(targetId);
                
                if (targetContainer) {
                    this.toggleFilterSection(toggle, targetContainer);
                }
            });
        });
    }

    /**
     * Toggle filter section visibility
     * @param {HTMLElement} toggle - Toggle button
     * @param {HTMLElement} container - Container to toggle
     */
    toggleFilterSection(toggle, container) {
        const isCollapsed = container.classList.contains('collapsed');
        
        if (isCollapsed) {
            // Expand
            container.classList.remove('collapsed');
            toggle.classList.add('expanded');
        } else {
            // Collapse
            container.classList.add('collapsed');
            toggle.classList.remove('expanded');
        }
    }

    /**
     * Setup range input event listeners
     */
    setupRangeInputs() {
        Object.entries(this.filterDefinitions).forEach(([filterKey, definition]) => {
            if (definition.type === 'range') {
                ['Min', 'Max'].forEach(bound => {
                    const element = document.getElementById(`${filterKey}${bound}`);
                    if (element) {
                        element.addEventListener('input', (e) => {
                            this.filterState[`${filterKey}${bound}`] = e.target.value;
                            this.debouncedApply();
                        });
                    }
                });
            }
        });
    }

    /**
     * Apply current filters and trigger callback
     */
    applyFilters() {
        this.updateAllFilterCounts();
        this.callbacks.onFilterChange(this.getFilterState());
    }

    /**
     * Update all filter count badges
     */
    updateAllFilterCounts() {
        if (!this.config.showCounts) return;

        Object.keys(this.filterDefinitions).forEach(filterKey => {
            const definition = this.filterDefinitions[filterKey];
            if (definition.type === 'checkbox-group') {
                this.updateFilterCount(filterKey);
            }
        });
    }

    /**
     * Clear all filters
     */
    clearAllFilters() {
        // Reset filter state
        this.initializeFilterState();

        // Reset UI elements
        const searchInput = document.getElementById('searchInput');
        if (searchInput) searchInput.value = '';

        // Reset checkboxes
        Object.keys(this.filterDefinitions).forEach(filterKey => {
            const definition = this.filterDefinitions[filterKey];
            if (definition.type === 'checkbox-group') {
                const container = document.getElementById(definition.containerId);
                if (container) {
                    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                        cb.checked = false;
                    });
                    this.updateFilterCount(filterKey);
                }
            }
        });

        // Reset range inputs
        Object.entries(this.filterDefinitions).forEach(([filterKey, definition]) => {
            if (definition.type === 'range') {
                ['Min', 'Max'].forEach(bound => {
                    const element = document.getElementById(`${filterKey}${bound}`);
                    if (element) element.value = '';
                });
            }
        });

        this.callbacks.onClear();
        this.applyFilters();
    }

    /**
     * Get current filter state
     * @returns {Object} Current filter state
     */
    getFilterState() {
        return { ...this.filterState };
    }

    /**
     * Set filter state (for context restoration)
     * @param {Object} state - Filter state to restore
     */
    setFilterState(state) {
        this.filterState = { ...state };
        this.updateUIFromState();
        this.applyFilters();
    }

    /**
     * Update UI elements to match current filter state
     */
    updateUIFromState() {
        // Update search input
        const searchInput = document.getElementById('searchInput');
        if (searchInput) {
            searchInput.value = this.filterState.search || '';
        }

        // Update checkboxes
        Object.entries(this.filterDefinitions).forEach(([filterKey, definition]) => {
            if (definition.type === 'checkbox-group') {
                const container = document.getElementById(definition.containerId);
                if (container) {
                    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                        cb.checked = this.filterState[filterKey]?.includes(cb.value) || false;
                    });
                    this.updateFilterCount(filterKey);
                }
            }
        });

        // Update range inputs
        Object.entries(this.filterDefinitions).forEach(([filterKey, definition]) => {
            if (definition.type === 'range') {
                ['Min', 'Max'].forEach(bound => {
                    const element = document.getElementById(`${filterKey}${bound}`);
                    if (element) {
                        element.value = this.filterState[`${filterKey}${bound}`] || '';
                    }
                });
            }
        });
    }

    /**
     * Debounce utility function
     * @param {Function} func - Function to debounce
     * @param {number} wait - Wait time in milliseconds
     * @returns {Function} Debounced function
     */
    debounce(func, wait) {
        let timeout;
        return function executedFunction(...args) {
            const later = () => {
                clearTimeout(timeout);
                func(...args);
            };
            clearTimeout(timeout);
            timeout = setTimeout(later, wait);
        };
    }
}

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = BaseFilters;
} else {
    window.BaseFilters = BaseFilters;
}