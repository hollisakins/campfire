/**
 * Base Table Component
 * Unified table functionality with pagination based on NIRCam's mature patterns
 */

class BaseTable {
    constructor(config = {}) {
        this.config = {
            tableId: config.tableId || 'dataTable',
            bodyId: config.bodyId || 'tableBody',
            paginationId: config.paginationId || 'pagination-controls',
            defaultItemsPerPage: config.defaultItemsPerPage || 10,
            itemsPerPageOptions: config.itemsPerPageOptions || [10, 25, 50, 100],
            sortable: config.sortable !== false,
            ...config
        };

        this.data = [];
        this.filteredData = [];
        this.sortColumn = null;
        this.sortDirection = 'asc';
        this.currentPage = 1;
        this.itemsPerPage = this.config.defaultItemsPerPage;
        
        this.callbacks = {
            onRowClick: config.onRowClick || (() => {}),
            onDataUpdate: config.onDataUpdate || (() => {}),
            renderRow: config.renderRow || this.defaultRenderRow.bind(this)
        };

        this.columnDefinitions = config.columns || [];
    }

    /**
     * Initialize table with data
     * @param {Array} data - Initial data array
     * @param {Array} columns - Column definitions
     */
    initialize(data, columns = null) {
        this.data = data || [];
        this.filteredData = [...this.data];
        
        if (columns) {
            this.columnDefinitions = columns;
        }

        this.setupEventListeners();
        this.renderTable();
        this.updateStats();
    }

    /**
     * Set table data and refresh
     * @param {Array} data - New data array
     */
    setData(data) {
        this.data = data || [];
        this.filteredData = [...this.data];
        this.currentPage = 1;
        this.renderTable();
        this.updateStats();
        this.callbacks.onDataUpdate(this.data);
    }

    /**
     * Apply filters to data
     * @param {Function} filterFunction - Function to filter data
     */
    applyFilters(filterFunction) {
        if (typeof filterFunction === 'function') {
            this.filteredData = this.data.filter(filterFunction);
        } else {
            this.filteredData = [...this.data];
        }
        
        this.currentPage = 1; // Reset to first page
        this.renderTable();
        this.updateStats();
    }

    /**
     * Setup event listeners for sorting, pagination, etc.
     */
    setupEventListeners() {
        // Sorting
        if (this.config.sortable) {
            this.setupSorting();
        }

        // Pagination
        this.setupPagination();

        // Row clicks
        this.setupRowClicks();
    }

    /**
     * Setup sorting functionality
     */
    setupSorting() {
        const table = document.getElementById(this.config.tableId);
        if (!table) return;

        table.addEventListener('click', (e) => {
            const th = e.target.closest('th.sortable');
            if (!th) return;

            const column = th.dataset.column;
            if (!column) return;

            if (this.sortColumn === column) {
                this.sortDirection = this.sortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                this.sortColumn = column;
                this.sortDirection = 'asc';
            }

            this.updateSortIndicators();
            this.renderTable();
        });
    }

    /**
     * Update sort indicators in table headers
     */
    updateSortIndicators() {
        const table = document.getElementById(this.config.tableId);
        if (!table) return;

        // Clear all indicators
        table.querySelectorAll('th.sortable').forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
        });

        // Add indicator to current sort column
        if (this.sortColumn) {
            const currentTh = table.querySelector(`th[data-column="${this.sortColumn}"]`);
            if (currentTh) {
                currentTh.classList.add(`sort-${this.sortDirection}`);
            }
        }
    }

    /**
     * Setup pagination controls (NIRCam style)
     */
    setupPagination() {
        // Items per page selector
        const itemsPerPageSelect = document.getElementById('items-per-page');
        if (itemsPerPageSelect) {
            itemsPerPageSelect.addEventListener('change', (e) => {
                this.itemsPerPage = parseInt(e.target.value);
                this.currentPage = 1;
                this.renderTable();
                this.updatePagination();
            });
        }

        // Pagination buttons
        const firstBtn = document.getElementById('first-page');
        const prevBtn = document.getElementById('prev-page');
        const nextBtn = document.getElementById('next-page');
        const lastBtn = document.getElementById('last-page');

        if (firstBtn) {
            firstBtn.addEventListener('click', () => {
                this.currentPage = 1;
                this.renderTable();
                this.updatePagination();
            });
        }

        if (prevBtn) {
            prevBtn.addEventListener('click', () => {
                if (this.currentPage > 1) {
                    this.currentPage--;
                    this.renderTable();
                    this.updatePagination();
                }
            });
        }

        if (nextBtn) {
            nextBtn.addEventListener('click', () => {
                const totalPages = Math.ceil(this.filteredData.length / this.itemsPerPage);
                if (this.currentPage < totalPages) {
                    this.currentPage++;
                    this.renderTable();
                    this.updatePagination();
                }
            });
        }

        if (lastBtn) {
            lastBtn.addEventListener('click', () => {
                const totalPages = Math.ceil(this.filteredData.length / this.itemsPerPage);
                this.currentPage = totalPages;
                this.renderTable();
                this.updatePagination();
            });
        }
    }

    /**
     * Setup row click handlers
     */
    setupRowClicks() {
        const tbody = document.getElementById(this.config.bodyId);
        if (!tbody) return;

        tbody.addEventListener('click', (e) => {
            const row = e.target.closest('tr[data-id]');
            if (!row) return;

            const id = row.dataset.id;
            const rowData = this.filteredData.find(item => 
                item.id === id || item.id === parseInt(id)
            );

            if (rowData) {
                this.callbacks.onRowClick(rowData, e);
            }
        });
    }

    /**
     * Sort filtered data
     */
    sortData() {
        if (!this.sortColumn) return;

        this.filteredData.sort((a, b) => {
            let aVal = a[this.sortColumn];
            let bVal = b[this.sortColumn];
            
            // Handle null/undefined
            if (aVal == null) aVal = '';
            if (bVal == null) bVal = '';
            
            // Compare
            let result = 0;
            if (typeof aVal === 'number' && typeof bVal === 'number') {
                result = aVal - bVal;
            } else {
                result = String(aVal).localeCompare(String(bVal));
            }
            
            return this.sortDirection === 'asc' ? result : -result;
        });
    }

    /**
     * Render table content
     */
    renderTable() {
        const tbody = document.getElementById(this.config.bodyId);
        if (!tbody) return;

        if (this.filteredData.length === 0) {
            this.renderEmptyState(tbody);
            this.updatePagination();
            return;
        }

        // Sort data
        this.sortData();

        // Paginate
        const start = (this.currentPage - 1) * this.itemsPerPage;
        const end = start + this.itemsPerPage;
        const pageData = this.filteredData.slice(start, end);

        // Render rows
        tbody.innerHTML = pageData.map(item => this.callbacks.renderRow(item)).join('');

        // Update pagination
        this.updatePagination();
    }

    /**
     * Render empty state
     * @param {HTMLElement} tbody - Table body element
     */
    renderEmptyState(tbody) {
        const colspan = this.columnDefinitions.length || 5;
        tbody.innerHTML = `
            <tr>
                <td colspan="${colspan}" class="text-center py-4">
                    <div class="text-muted">
                        <p>No data matches the current filters</p>
                    </div>
                </td>
            </tr>
        `;
    }

    /**
     * Default row renderer
     * @param {Object} item - Data item
     * @returns {string} HTML row string
     */
    defaultRenderRow(item) {
        const cells = this.columnDefinitions.map(col => {
            const value = item[col.field] || '';
            return `<td class="${col.className || ''}">${value}</td>`;
        }).join('');

        return `<tr data-id="${item.id || ''}" class="data-row">${cells}</tr>`;
    }

    /**
     * Update pagination controls (NIRCam style)
     */
    updatePagination() {
        const totalPages = Math.ceil(this.filteredData.length / this.itemsPerPage);
        const start = (this.currentPage - 1) * this.itemsPerPage + 1;
        const end = Math.min(this.currentPage * this.itemsPerPage, this.filteredData.length);

        // Update page info
        const pageInfo = document.getElementById('page-info');
        if (pageInfo) {
            if (this.filteredData.length === 0) {
                pageInfo.textContent = '0-0 of 0';
            } else {
                pageInfo.textContent = `${start}-${end} of ${this.filteredData.length}`;
            }
        }

        // Update button states
        const firstBtn = document.getElementById('first-page');
        const prevBtn = document.getElementById('prev-page');
        const nextBtn = document.getElementById('next-page');
        const lastBtn = document.getElementById('last-page');

        if (firstBtn) firstBtn.disabled = this.currentPage === 1;
        if (prevBtn) prevBtn.disabled = this.currentPage === 1;
        if (nextBtn) nextBtn.disabled = this.currentPage === totalPages || totalPages === 0;
        if (lastBtn) lastBtn.disabled = this.currentPage === totalPages || totalPages === 0;

        // Show/hide pagination controls
        const paginationContainer = document.getElementById(this.config.paginationId);
        if (paginationContainer) {
            paginationContainer.style.display = totalPages > 1 ? 'flex' : 'none';
        }
    }

    /**
     * Update statistics display
     */
    updateStats() {
        // This can be overridden by subclasses for specific stats display
        const totalItems = this.data.length;
        const filteredItems = this.filteredData.length;
        
        // Look for common stats elements
        const statsElement = document.getElementById('filter-summary') || 
                            document.getElementById('table-stats') ||
                            document.getElementById('visible-count');
        
        if (statsElement) {
            if (statsElement.id === 'visible-count') {
                statsElement.textContent = filteredItems;
            } else {
                statsElement.textContent = `Showing ${filteredItems} of ${totalItems} items`;
            }
        }
    }

    /**
     * Get currently visible (paginated) data
     * @returns {Array} Current page data
     */
    getVisibleData() {
        const start = (this.currentPage - 1) * this.itemsPerPage;
        const end = start + this.itemsPerPage;
        return this.filteredData.slice(start, end);
    }

    /**
     * Get all filtered data
     * @returns {Array} All filtered data
     */
    getFilteredData() {
        return [...this.filteredData];
    }

    /**
     * Show loading state
     */
    showLoading() {
        const tbody = document.getElementById(this.config.bodyId);
        if (!tbody) return;

        const colspan = this.columnDefinitions.length || 5;
        tbody.innerHTML = `
            <tr>
                <td colspan="${colspan}" class="text-center py-4">
                    <div class="loading-container">
                        <div class="spinner"></div>
                        <p class="mt-2">Loading data...</p>
                    </div>
                </td>
            </tr>
        `;
    }

    /**
     * Show error state
     * @param {string} message - Error message to display
     */
    showError(message) {
        const tbody = document.getElementById(this.config.bodyId);
        if (!tbody) return;

        const colspan = this.columnDefinitions.length || 5;
        tbody.innerHTML = `
            <tr>
                <td colspan="${colspan}" class="text-center py-4">
                    <div class="text-danger">
                        <p>Error: ${message}</p>
                        <p class="small">Please refresh the page to try again.</p>
                    </div>
                </td>
            </tr>
        `;
    }
}

// Export for use in other modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = BaseTable;
} else {
    window.BaseTable = BaseTable;
}