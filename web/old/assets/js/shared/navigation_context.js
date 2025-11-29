/**
 * Navigation context management for maintaining state between pages
 */

class NavigationContext {
    /**
     * Encode filter/sort state to base64 for URL
     */
    static encode(state) {
        try {
            return btoa(JSON.stringify(state));
        } catch (error) {
            console.warn('Failed to encode navigation context:', error);
            return '';
        }
    }
    
    /**
     * Decode base64 state from URL
     */
    static decode(encoded) {
        try {
            return JSON.parse(atob(encoded));
        } catch (error) {
            console.warn('Failed to decode navigation context:', error);
            return null;
        }
    }
    
    /**
     * Create object detail URL with preserved context
     */
    static createObjectURL(objectId, currentState) {
        if (!currentState) {
            return `nirspec_object.html?id=${objectId}`;
        }
        
        const encoded = this.encode(currentState);
        return encoded ? 
            `nirspec_object.html?id=${objectId}&context=${encoded}` :
            `nirspec_object.html?id=${objectId}`;
    }
    
    /**
     * Create return URL to main table with restored context
     */
    static createReturnURL(context) {
        if (!context) {
            return 'nirspec.html';
        }
        
        const encoded = this.encode(context);
        return encoded ? 
            `nirspec.html?restored=${encoded}` :
            'nirspec.html';
    }
    
    /**
     * Get navigation list based on context and master data
     */
    static getNavigationList(currentId, context, masterData) {
        if (!context || !masterData) {
            return {
                previous: null,
                next: null,
                position: '- of -'
            };
        }
        
        // Apply filters from context to master data
        const filtered = this.applyFilters(masterData, context.filters || {});
        const sorted = this.applySort(filtered, context.sort || { column: 'id', direction: 'asc' });
        
        // Find current position
        const currentIndex = sorted.findIndex(obj => obj.id === currentId);
        
        if (currentIndex === -1) {
            return {
                previous: null,
                next: null,
                position: '- of -'
            };
        }
        
        return {
            previous: currentIndex > 0 ? sorted[currentIndex - 1].id : null,
            next: currentIndex < sorted.length - 1 ? sorted[currentIndex + 1].id : null,
            position: `${currentIndex + 1} of ${sorted.length}`
        };
    }
    
    /**
     * Apply filters to data array
     */
    static applyFilters(data, filters) {
        return data.filter(obj => {
            // Search filter
            if (filters.search) {
                const searchLower = filters.search.toLowerCase();
                const searchFields = [
                    obj.id,
                    obj.observation,
                    obj.field,
                    obj.program,
                    ...(obj.gratings || [])
                ].filter(Boolean).map(s => s.toLowerCase());
                
                if (!searchFields.some(field => field.includes(searchLower))) {
                    return false;
                }
            }
            
            // Multi-select filters
            if (filters.observation && filters.observation.length > 0 && 
                !filters.observation.includes(obj.observation)) {
                return false;
            }
            
            if (filters.field && filters.field.length > 0 && 
                !filters.field.includes(obj.field)) {
                return false;
            }
            
            if (filters.grating && filters.grating.length > 0) {
                const objGratings = obj.gratings || [];
                if (!filters.grating.some(g => objGratings.includes(g))) {
                    return false;
                }
            }
            
            if (filters.quality && filters.quality.length > 0 && 
                !filters.quality.includes(String(obj.redshift_quality))) {
                return false;
            }
            
            // Range filters
            if (filters.redshiftMin !== '' && filters.redshiftMin !== undefined && 
                obj.redshift < parseFloat(filters.redshiftMin)) {
                return false;
            }
            
            if (filters.redshiftMax !== '' && filters.redshiftMax !== undefined && 
                obj.redshift > parseFloat(filters.redshiftMax)) {
                return false;
            }
            
            if (filters.raMin !== '' && filters.raMin !== undefined && 
                obj.ra < parseFloat(filters.raMin)) {
                return false;
            }
            
            if (filters.raMax !== '' && filters.raMax !== undefined && 
                obj.ra > parseFloat(filters.raMax)) {
                return false;
            }
            
            if (filters.decMin !== '' && filters.decMin !== undefined && 
                obj.dec < parseFloat(filters.decMin)) {
                return false;
            }
            
            if (filters.decMax !== '' && filters.decMax !== undefined && 
                obj.dec > parseFloat(filters.decMax)) {
                return false;
            }
            
            return true;
        });
    }
    
    /**
     * Apply sorting to data array
     */
    static applySort(data, sort) {
        const { column, direction } = sort;
        
        return [...data].sort((a, b) => {
            let aVal = a[column];
            let bVal = b[column];
            
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
            
            return direction === 'asc' ? result : -result;
        });
    }
    
    /**
     * Restore context on page load if available
     */
    static restoreContextOnLoad() {
        const params = new URLSearchParams(window.location.search);
        const restoredParam = params.get('restored');
        
        if (restoredParam) {
            const context = this.decode(restoredParam);
            if (context && window.nirspecFilters) {
                // Restore filter state
                window.nirspecFilters.setState(context.filters || {});
                
                // Restore table state
                if (window.nirspecTable) {
                    if (context.sort) {
                        window.nirspecTable.sortColumn = context.sort.column;
                        window.nirspecTable.sortDirection = context.sort.direction;
                    }
                    if (context.page) {
                        window.nirspecTable.currentPage = context.page;
                    }
                    if (context.rowsPerPage) {
                        window.nirspecTable.rowsPerPage = context.rowsPerPage;
                        document.getElementById('rowsPerPage').value = context.rowsPerPage;
                    }
                }
                
                // Clean URL
                window.history.replaceState({}, document.title, 'nirspec.html');
            }
        }
    }
}

// Auto-restore context when page loads
document.addEventListener('DOMContentLoaded', () => {
    // Wait a bit for other components to initialize
    setTimeout(() => {
        NavigationContext.restoreContextOnLoad();
    }, 100);
});

// Make available globally
window.NavigationContext = NavigationContext;