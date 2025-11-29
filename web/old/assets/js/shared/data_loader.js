/**
 * Shared data loading utilities for CAMPFIRE
 */

class DataLoader {
    constructor() {
        this.cache = new Map();
        this.loadingPromises = new Map();
    }
    
    /**
     * Load NIRSpec manifest with caching
     */
    async loadNIRSpecManifest() {
        return this.loadJSON('nirspec_manifest', 'static/manifests/merged_manifest.json');
    }
    
    /**
     * Load NIRSpec filter options
     */
    async loadNIRSpecFilters() {
        return this.loadJSON('nirspec_filters', 'static/manifests/filters.json');
    }
    
    /**
     * Load NIRSpec statistics
     */
    async loadNIRSpecStats() {
        return this.loadJSON('nirspec_stats', 'static/manifests/stats.json');
    }
    
    /**
     * Load NIRSpec program configuration
     */
    async loadNIRSpecPrograms() {
        return this.loadTOML('nirspec_programs', 'static/config/programs.toml');
    }

    /**
     * Load NIRCam TOML manifest
     * @param {string} field - Field name (cosmos, uds, etc.)
     */
    async loadNIRCamManifest(field) {
        return this.loadTOML(`nircam_manifest_${field}`, `static/manifests/nircam-${field}-manifest.toml`);
    }

    /**
     * Load markdown content (for reduction notes)
     * @param {string} field - Field name
     */
    async loadReductionNotes(field) {
        return this.loadText(`reduction_notes_${field}`, `build/docs/nircam-${field}-reduction-notes.md`);
    }
    
    /**
     * Generic JSON loader with caching and deduplication
     */
    async loadJSON(cacheKey, url) {
        // Return cached data if available
        if (this.cache.has(cacheKey)) {
            return this.cache.get(cacheKey);
        }
        
        // Prevent duplicate requests
        if (this.loadingPromises.has(cacheKey)) {
            return this.loadingPromises.get(cacheKey);
        }
        
        // Create loading promise
        const loadPromise = fetch(url)
            .then(res => {
                if (!res.ok) {
                    throw new Error(`Failed to load ${url}: ${res.statusText}`);
                }
                return res.json();
            })
            .then(data => {
                // Cache the data
                this.cache.set(cacheKey, data);
                this.loadingPromises.delete(cacheKey);
                return data;
            })
            .catch(error => {
                this.loadingPromises.delete(cacheKey);
                console.error(`Error loading ${url}:`, error);
                throw error;
            });
        
        this.loadingPromises.set(cacheKey, loadPromise);
        return loadPromise;
    }
    
    /**
     * Generic TOML loader with caching and deduplication
     */
    async loadTOML(cacheKey, url) {
        // Return cached data if available
        if (this.cache.has(cacheKey)) {
            return this.cache.get(cacheKey);
        }
        
        // Prevent duplicate requests
        if (this.loadingPromises.has(cacheKey)) {
            return this.loadingPromises.get(cacheKey);
        }
        
        // Create loading promise
        const loadPromise = fetch(url)
            .then(res => {
                if (!res.ok) {
                    throw new Error(`Failed to load ${url}: ${res.statusText}`);
                }
                return res.text();
            })
            .then(tomlText => {
                // Parse TOML using the TOML library
                if (typeof TOML === 'undefined') {
                    throw new Error('TOML parser not available. Make sure toml-parser.js is loaded.');
                }
                const data = TOML.parse(tomlText);
                
                // Cache the data
                this.cache.set(cacheKey, data);
                this.loadingPromises.delete(cacheKey);
                return data;
            })
            .catch(error => {
                this.loadingPromises.delete(cacheKey);
                console.error(`Error loading ${url}:`, error);
                throw error;
            });
        
        this.loadingPromises.set(cacheKey, loadPromise);
        return loadPromise;
    }

    /**
     * Generic text loader with caching and deduplication
     */
    async loadText(cacheKey, url) {
        // Return cached data if available
        if (this.cache.has(cacheKey)) {
            return this.cache.get(cacheKey);
        }
        
        // Prevent duplicate requests
        if (this.loadingPromises.has(cacheKey)) {
            return this.loadingPromises.get(cacheKey);
        }
        
        // Create loading promise
        const loadPromise = fetch(url)
            .then(res => {
                if (!res.ok) {
                    throw new Error(`Failed to load ${url}: ${res.statusText}`);
                }
                return res.text();
            })
            .then(text => {
                // Cache the data
                this.cache.set(cacheKey, text);
                this.loadingPromises.delete(cacheKey);
                return text;
            })
            .catch(error => {
                this.loadingPromises.delete(cacheKey);
                console.error(`Error loading ${url}:`, error);
                throw error;
            });
        
        this.loadingPromises.set(cacheKey, loadPromise);
        return loadPromise;
    }

    /**
     * Clear cache for a specific key or all cached data
     */
    clearCache(key = null) {
        if (key) {
            this.cache.delete(key);
        } else {
            this.cache.clear();
        }
    }
}

// Create global instance
window.dataLoader = new DataLoader();