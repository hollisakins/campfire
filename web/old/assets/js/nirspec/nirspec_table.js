/**
 * NIRSpec table functionality - Enhanced with shared components
 */

class NIRSpecTable extends BaseTable {
    constructor() {
        const config = {
            tableId: 'dataTable',
            bodyId: 'tableBody',
            paginationId: 'pagination-controls',
            defaultItemsPerPage: 25,
            itemsPerPageOptions: [25, 50, 100, 250],
            columns: [
                { field: 'id', header: 'Object ID', sortable: true },
                { field: 'program', header: 'Program', sortable: true },
                { field: 'field', header: 'Field', sortable: true },
                { field: 'ra', header: 'RA', sortable: true },
                { field: 'dec', header: 'Dec', sortable: true },
                { field: 'redshift', header: 'Redshift', sortable: true },
                { field: 'redshift_quality', header: 'Quality', sortable: false },
                { field: 'max_snr', header: 'Max S/N', sortable: true },
                { field: 'gratings', header: 'Gratings', sortable: false },
                { field: 'thumbnail', header: 'Preview', sortable: false }
            ]
        };

        super(config);
        
        // Set the custom row renderer after super() call
        this.callbacks.renderRow = this.renderRow.bind(this);
        
        this.selectedObjects = new Set();
        this.stats = null;
        this.filters = null;
        
        this.init();
    }
    
    async init() {
        try {
            // Show loading state
            this.showLoading();
            
            // Load data
            const [manifest, filters, stats] = await Promise.all([
                window.dataLoader.loadNIRSpecManifest(),
                window.dataLoader.loadNIRSpecFilters(),
                window.dataLoader.loadNIRSpecStats()
            ]);
            
            this.filters = filters;
            this.stats = stats;
            
            // Process objects to extract max S/N values
            const processedObjects = (manifest.objects || []).map(obj => {
                // Extract best max S/N from grating metadata
                const maxSnr = this.extractMaxSnr(obj);
                return { ...obj, max_snr: maxSnr };
            });
            
            // Initialize table with data using base class
            this.initialize(processedObjects, this.config.columns);
            
            // Store processed data for filters to access
            this.processedData = processedObjects;
            
            // Setup NIRSpec-specific functionality
            this.updateStats();
            
            // Populate filter options with the processed data (includes max_snr)
            if (window.nirspecFilters) {
                window.nirspecFilters.populateFilterOptions(processedObjects);
            }
            
        } catch (error) {
            console.error('Failed to initialize NIRSpec table:', error);
            this.showError('Failed to load data. Please refresh the page.');
        }
    }
    
    // showLoading and showError are now inherited from BaseTable
    
    updateStats() {
        // Override base class updateStats with NIRSpec-specific stats
        const totalObjects = this.data.length;
        const filteredObjects = this.filteredData.length;
        const totalSpectra = this.stats?.total_spectra || '-';
        const lastUpdated = this.stats?.generated ? 
            new Date(this.stats.generated).toLocaleString() : '-';
        
        // Update filter summary
        const filterSummary = document.getElementById('filter-summary');
        if (filterSummary) {
            filterSummary.textContent = `Selected: ${filteredObjects} of ${totalObjects} spectra`;
        }

        // Update stats footer - just show last updated
        const tableStatsText = document.getElementById('tableStatsText');
        if (tableStatsText) {
            tableStatsText.textContent = `Last updated: ${lastUpdated}`;
        }

        const footerLastUpdated = document.getElementById('footerLastUpdated');
        if (footerLastUpdated) {
            footerLastUpdated.textContent = lastUpdated;
        }
    }
    
    // renderTable is now inherited from BaseTable
    
    renderRow(obj) {
        const qualityBadge = this.getQualityBadge(obj.redshift_quality);
        
        // Display grating emojis
        const GRATING_EMOJI = {
            'prism-clear': '🌈',      // Rainbow for PRISM
            'g395m-f290lp': '🔴'      // Red circle for G395M
        };
        
        const gratingEmojis = (obj.gratings || [])
            .map(g => GRATING_EMOJI[g] || '❓')
            .join('');
        const gratingDisplay = gratingEmojis || '-';
        
        // Format S/N with color coding
        const snrDisplay = this.formatSnr(obj.max_snr);
        
        // Generate object detail page URL with context
        const objectUrl = this.generateObjectUrl(obj.id);
        
        // New column order: Object ID, Program, Field, RA, Dec, Redshift, Quality, Max S/N, Gratings, Thumbnail
        return `
            <tr data-id="${obj.id}" class="data-row">
                <td class="object-id-cell">
                    <a href="${objectUrl}" class="object-link">${obj.id}</a>
                </td>
                <td>${obj.program || '-'}</td>
                <td>${obj.field || '-'}</td>
                <td>${obj.ra?.toFixed(4) || '-'}</td>
                <td>${obj.dec?.toFixed(4) || '-'}</td>
                <td>${obj.redshift?.toFixed(3) || '-'}</td>
                <td>${qualityBadge}</td>
                <td>${snrDisplay}</td>
                <td>${gratingDisplay}</td>
                <td>
                    ${obj.files?.thumbnail ? 
                        `<img src="data/nirspec/${obj.files.thumbnail}" 
                              alt="${obj.id}" 
                              class="thumbnail" 
                              loading="lazy">` : 
                        '<div class="thumbnail-placeholder">-</div>'}
                </td>
            </tr>
        `;
    }

    generateObjectUrl(objectId) {
        // Encode current filter state for context preservation
        const filterState = window.nirspecFilters?.getFilterState() || {};
        const context = {
            filters: filterState,
            sort: { 
                column: this.sortColumn || 'id',  // Default to 'id' if no sort column
                direction: this.sortDirection || 'asc' 
            },
            page: this.currentPage,
            rowsPerPage: this.itemsPerPage
        };
        
        // Debug logging can be uncommented if needed
        // console.log('Generating object URL with context:', context);
        // console.log('Filter state:', filterState);
        // console.log('nirspecFilters available:', !!window.nirspecFilters);
        
        const contextParam = btoa(JSON.stringify(context));
        return `nirspec_object.html?id=${objectId}&context=${contextParam}`;
    }
    
    getQualityBadge(quality) {
        // Handle both old (-1,0,1,2) and new (0-4) quality scales
        if (!window.flagManager?.initialized) {
            // Fallback for old system or before flag manager loads
            const oldFlags = {
                '-1': { color: '#dc3545', label: 'BAD' },
                '0': { color: '#6c757d', label: 'NONE' },
                '1': { color: '#ffc107', label: 'AUTO' },
                '2': { color: '#28a745', label: 'GOOD' }
            };
            const flag = oldFlags[String(quality)];
            if (!flag) return '-';
            return `<span class="badge" style="background-color: ${flag.color}; color: white;">${flag.label}</span>`;
        }
        
        try {
            // Use quality value directly - no migration needed since we're using the new system
            const qualityInfo = window.flagManager.getQualityInfo(quality);
            
            return `<span class="badge" style="background-color: transparent; color: inherit; border: none; font-size: 1.2em;" title="${qualityInfo.description}">${qualityInfo.icon}</span>`;
        } catch (error) {
            console.warn('Failed to get quality badge:', error);
            return '-';
        }
    }
    
    extractMaxSnr(obj) {
        // Priority: PRISM-CLEAR > any other grating
        let maxSnr = null;
        
        // Check PRISM-CLEAR first
        if (obj.files && obj.files['prism-clear'] && 
            obj.files['prism-clear'].metadata && 
            obj.files['prism-clear'].metadata.max_snr) {
            maxSnr = obj.files['prism-clear'].metadata.max_snr;
        }
        
        // If no PRISM-CLEAR, check other gratings
        if (maxSnr === null && obj.files) {
            for (const grating of (obj.gratings || [])) {
                if (obj.files[grating] && 
                    obj.files[grating].metadata && 
                    obj.files[grating].metadata.max_snr) {
                    maxSnr = obj.files[grating].metadata.max_snr;
                    break;
                }
            }
        }
        
        return maxSnr;
    }
    
    formatSnr(snr) {
        if (snr === null || snr === undefined) {
            return '<span class="snr-none">-</span>';
        }
        
        let className = 'snr-high';  // Default green for > 10
        if (snr < 5) {
            className = 'snr-low';    // Red for < 5
        } else if (snr < 10) {
            className = 'snr-medium'; // Yellow for 5-10
        }
        
        return `<span class="${className}">${snr.toFixed(1)}</span>`;
    }
    
    // sortData, updatePagination, and most setupEventListeners functionality now inherited from BaseTable
    
    setupEventListeners() {
        // Call parent class setup first
        super.setupEventListeners();
        
        // No need for additional event listeners since we're using direct links now
        // The object links will handle navigation to detail pages automatically
    }
    
    viewObject(objectId) {
        // Encode current filter state
        const context = {
            filters: window.nirspecFilters?.getState() || {},
            sort: { column: this.sortColumn, direction: this.sortDirection },
            page: this.currentPage,
            rowsPerPage: this.rowsPerPage
        };
        
        const contextParam = btoa(JSON.stringify(context));
        window.location.href = `nirspec_object.html?id=${objectId}&context=${contextParam}`;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.nirspecTable = new NIRSpecTable();
});