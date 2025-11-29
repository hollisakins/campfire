/**
 * NIRSpec filtering functionality - Enhanced with shared components
 */

class NIRSpecFilters extends BaseFilters {
    constructor() {
        const config = {
            searchable: true,
            collapsible: true,
            showCounts: true,
            onFilterChange: (filterState) => {
                this.applyNIRSpecFilters(filterState);
            },
            onClear: () => {
                this.clearNIRSpecFilters();
            }
        };

        super(config);

        // NIRSpec-specific filter definitions
        const filterDefinitions = {
            observation: {
                type: 'checkbox-group',
                containerId: 'observation-checkboxes',
                dataField: 'observation'
            },
            field: {
                type: 'checkbox-group', 
                containerId: 'field-checkboxes',
                dataField: 'field'
            },
            grating: {
                type: 'checkbox-group',
                containerId: 'grating-checkboxes',
                dataField: 'gratings',  // This is an array field
                optionLabels: {
                    'prism-clear': '🌈 PRISM-CLEAR',
                    'g395m-f290lp': '🔴 G395M-F290LP'
                }
            },
            quality: {
                type: 'checkbox-group',
                containerId: 'quality-checkboxes',
                staticOptions: ['4', '3', '2', '1', '0'],
                optionLabels: {
                    '0': '⚪️ Not Inspected',
                    '1': '🔴 Bad',
                    '2': '🟠 Tentative',
                    '3': '🟡 Probable',
                    '4': '🟢 Secure'
                }
            },
            program: {
                type: 'checkbox-group',
                containerId: 'program-checkboxes',
                dataField: 'program'
            }
        };

        this.downloadGenerator = new DownloadGenerator({
            baseUrl: 'https://data.hollisakins.com'
        });
        
        // Additional properties for S/N range and flags
        this.snrRange = { min: 0, max: 50 };
        this.spectralFeaturesOptions = [];
        this.objectFlagsOptions = [];
        this.dqFlagsOptions = [];
        
        this.setupNIRSpecFilters(filterDefinitions);
    }

    setupNIRSpecFilters(filterDefinitions) {
        // Initialize with empty data first - will be populated when data loads
        this.initialize(filterDefinitions, []);
        
        // Setup additional NIRSpec-specific event listeners
        this.setupDownloadControls();
        this.setupSnrRangeFilter();
        this.setupFlagFilters();
    }
    
    setupSnrRangeFilter() {
        const minSlider = document.getElementById('snr-min-slider');
        const maxSlider = document.getElementById('snr-max-slider');
        const snrCount = document.getElementById('snr-count');
        
        if (!minSlider || !maxSlider) return;
        
        const updateSnrRange = () => {
            const min = parseFloat(minSlider.value);
            const max = parseFloat(maxSlider.value);
            
            // Ensure min doesn't exceed max
            if (min > max) {
                if (this.snrRange.min !== min) {
                    maxSlider.value = min;
                } else {
                    minSlider.value = max;
                }
            }
            
            this.snrRange.min = parseFloat(minSlider.value);
            this.snrRange.max = parseFloat(maxSlider.value);
            
            // Update count display - compare to data range, not hardcoded values
            const isFiltered = this.snrDataRange && 
                (this.snrRange.min > this.snrDataRange.min || 
                 this.snrRange.max < this.snrDataRange.max);
            
            if (isFiltered) {
                snrCount.textContent = `(${this.snrRange.min.toFixed(1)}-${this.snrRange.max.toFixed(1)})`;
                snrCount.style.display = 'inline';
            } else {
                snrCount.style.display = 'none';
            }
            
            // Trigger filter update through base class
            this.applyFilters();
        };
        
        minSlider.addEventListener('input', updateSnrRange);
        maxSlider.addEventListener('input', updateSnrRange);
        
        // Toggle for S/N range is handled by base class since we're using checkbox-group class
    }
    
    setupFlagFilters() {
        // Setup spectral features filter
        this.setupFlagFilterGroup('spectral-features', 'spectral_features');
        
        // Setup object flags filter
        this.setupFlagFilterGroup('object-flags', 'object_flags');
        
        // Setup DQ flags filter  
        this.setupFlagFilterGroup('dq-flags', 'dq_flags');
    }
    
    setupFlagFilterGroup(idPrefix, flagType) {
        const container = document.getElementById(`${idPrefix}-checkboxes`);
        const count = document.getElementById(`${idPrefix}-count`);
        
        console.log(`Setting up flag filter group: ${idPrefix}, container found:`, !!container);
        
        if (!container) {
            console.warn(`Container not found for ${idPrefix}`);
            return;
        }
        
        // Store reference for later population (toggle behavior handled by BaseFilters)
        if (flagType === 'object_flags') {
            this.objectFlagsContainer = container;
            this.objectFlagsCount = count;
        } else if (flagType === 'dq_flags') {
            this.dqFlagsContainer = container;
            this.dqFlagsCount = count;
        } else if (flagType === 'spectral_features') {
            this.spectralFeaturesContainer = container;
            this.spectralFeaturesCount = count;
        }
    }
    
    setupDownloadControls() {
        // Download filtered data button
        const downloadBtn = document.getElementById('downloadFilteredBtn');
        if (downloadBtn) {
            downloadBtn.addEventListener('click', () => {
                this.downloadFilteredData();
            });
        }
    }
    
    populateFilterOptions(data) {
        // Call base class method first
        super.populateFilterOptions(data);
        
        // Extract S/N range from data (data already has max_snr processed)
        const snrValues = data
            .map(obj => obj.max_snr)
            .filter(v => v !== null && v !== undefined);
        
        if (snrValues.length > 0) {
            const minSnr = Math.floor(Math.min(...snrValues));
            const maxSnr = Math.ceil(Math.max(...snrValues));
            
            // Update slider ranges
            const minSlider = document.getElementById('snr-min-slider');
            const maxSlider = document.getElementById('snr-max-slider');
            if (minSlider && maxSlider) {
                minSlider.min = minSnr;
                minSlider.max = maxSnr;
                minSlider.value = minSnr;
                maxSlider.min = minSnr;
                maxSlider.max = maxSnr;
                maxSlider.value = maxSnr;
                
                this.snrRange = { min: minSnr, max: maxSnr };
                
                // Store the data range for resetting
                this.snrDataRange = { min: minSnr, max: maxSnr };
            }
        }
        
        // Populate flag filters from actual data
        this.populateFlagOptions(data);
    }
    
    async populateFlagOptions(data) {
        console.log('PopulateFlagOptions called with', data.length, 'objects');
        console.log('Container references:', {
            objectFlags: !!this.objectFlagsContainer,
            dqFlags: !!this.dqFlagsContainer
        });
        
        // Ensure flag manager is initialized
        if (!window.flagManager?.initialized) {
            await window.flagManager?.initialize();
        }
        
        // Get unique flags from data - handle both string arrays and bitmasks
        const objectFlags = new Set();
        const dqFlags = new Set();
        const spectralFeatures = new Set();
        
        data.forEach(obj => {
            if (obj.collaborative) {
                // Handle old string format
                if (Array.isArray(obj.collaborative.object_flags)) {
                    obj.collaborative.object_flags.forEach(flag => objectFlags.add(flag));
                } else if (typeof obj.collaborative.obj_flags_bitmask === 'number') {
                    // Handle new bitmask format
                    try {
                        const flags = window.flagManager.decodeBitmask('object_flags', obj.collaborative.obj_flags_bitmask);
                        flags.forEach(flag => objectFlags.add(flag));
                    } catch (error) {
                        console.warn('Failed to decode object flags bitmask:', error);
                    }
                }
                
                // Handle DQ flags similarly
                if (Array.isArray(obj.collaborative.dq_flags)) {
                    obj.collaborative.dq_flags.forEach(flag => dqFlags.add(flag));
                } else if (typeof obj.collaborative.dq_flags_bitmask === 'number') {
                    // Handle new bitmask format
                    try {
                        const flags = window.flagManager.decodeBitmask('dq_flags', obj.collaborative.dq_flags_bitmask);
                        flags.forEach(flag => dqFlags.add(flag));
                    } catch (error) {
                        console.warn('Failed to decode DQ flags bitmask:', error);
                    }
                }
                
                // Handle spectral features bitmask
                if (typeof obj.collaborative.z_features === 'number') {
                    try {
                        const features = window.flagManager.decodeBitmask('spectral_features', obj.collaborative.z_features);
                        features.forEach(feature => spectralFeatures.add(feature));
                    } catch (error) {
                        console.warn('Failed to decode spectral features bitmask:', error);
                    }
                }
            }
        });
        
        console.log('Extracted flags:', {
            objectFlags: [...objectFlags],
            dqFlags: [...dqFlags],
            spectralFeatures: [...spectralFeatures]
        });
        
        // Populate object flags
        if (this.objectFlagsContainer && window.flagManager?.initialized) {
            console.log('Populating object flags container with', objectFlags.size, 'flags');
            this.objectFlagsContainer.innerHTML = '';
            this.objectFlagsContainer.className = 'checkbox-group collapsed flag-chips-container';
            const sortedFlags = Array.from(objectFlags).sort();
            
            sortedFlags.forEach(flag => {
                try {
                    const def = window.flagManager.getFlagDefinition('object_flags', flag);
                    const chip = document.createElement('button');
                    chip.className = 'flag-chip';
                    chip.style.background = def.color;
                    chip.dataset.flag = flag;
                    chip.dataset.selected = 'false';
                    chip.title = def.description || flag;
                    chip.innerHTML = `
                        <span class="flag-icon">${def.icon}</span>
                        <span>${def.label}</span>
                    `;
                    
                    // Add click event listener
                    chip.addEventListener('click', () => {
                        const isSelected = chip.dataset.selected === 'true';
                        chip.dataset.selected = (!isSelected).toString();
                        chip.classList.toggle('selected', !isSelected);
                        this.updateFlagFilter('object_flags');
                    });
                    
                    this.objectFlagsContainer.appendChild(chip);
                } catch (error) {
                    console.warn(`Failed to get definition for flag '${flag}':`, error);
                    // Fallback chip
                    const chip = document.createElement('button');
                    chip.className = 'flag-chip';
                    chip.style.background = '#e0e0e0';
                    chip.dataset.flag = flag;
                    chip.dataset.selected = 'false';
                    chip.title = flag;
                    chip.textContent = flag;
                    
                    chip.addEventListener('click', () => {
                        const isSelected = chip.dataset.selected === 'true';
                        chip.dataset.selected = (!isSelected).toString();
                        chip.classList.toggle('selected', !isSelected);
                        this.updateFlagFilter('object_flags');
                    });
                    
                    this.objectFlagsContainer.appendChild(chip);
                }
            });
        }
        
        // Populate DQ flags
        if (this.dqFlagsContainer && window.flagManager?.initialized) {
            console.log('Populating DQ flags container with', dqFlags.size, 'flags');
            this.dqFlagsContainer.innerHTML = '';
            this.dqFlagsContainer.className = 'checkbox-group collapsed flag-chips-container';
            const sortedFlags = Array.from(dqFlags).sort();
            
            sortedFlags.forEach(flag => {
                try {
                    const def = window.flagManager.getFlagDefinition('dq_flags', flag);
                    const chip = document.createElement('button');
                    chip.className = 'flag-chip';
                    chip.style.background = def.color;
                    chip.dataset.flag = flag;
                    chip.dataset.selected = 'false';
                    chip.title = def.description || flag;
                    chip.innerHTML = `
                        <span class="flag-icon">${def.icon}</span>
                        <span>${def.label}</span>
                    `;
                    
                    // Add click event listener
                    chip.addEventListener('click', () => {
                        const isSelected = chip.dataset.selected === 'true';
                        chip.dataset.selected = (!isSelected).toString();
                        chip.classList.toggle('selected', !isSelected);
                        this.updateFlagFilter('dq_flags');
                    });
                    
                    this.dqFlagsContainer.appendChild(chip);
                } catch (error) {
                    console.warn(`Failed to get definition for DQ flag '${flag}':`, error);
                    // Fallback chip
                    const chip = document.createElement('button');
                    chip.className = 'flag-chip';
                    chip.style.background = '#e0e0e0';
                    chip.dataset.flag = flag;
                    chip.dataset.selected = 'false';
                    chip.title = flag;
                    chip.textContent = flag;
                    
                    chip.addEventListener('click', () => {
                        const isSelected = chip.dataset.selected === 'true';
                        chip.dataset.selected = (!isSelected).toString();
                        chip.classList.toggle('selected', !isSelected);
                        this.updateFlagFilter('dq_flags');
                    });
                    
                    this.dqFlagsContainer.appendChild(chip);
                }
            });
        }
        
        // Populate spectral features
        if (this.spectralFeaturesContainer && window.flagManager?.initialized) {
            console.log('Populating spectral features container with', spectralFeatures.size, 'features');
            this.spectralFeaturesContainer.innerHTML = '';
            this.spectralFeaturesContainer.className = 'checkbox-group collapsed flag-chips-container';
            const sortedFeatures = Array.from(spectralFeatures).sort();
            
            sortedFeatures.forEach(feature => {
                try {
                    const def = window.flagManager.getFlagDefinition('spectral_features', feature);
                    const chip = document.createElement('button');
                    chip.className = 'flag-chip';
                    chip.style.background = def.color;
                    chip.dataset.flag = feature;
                    chip.dataset.selected = 'false';
                    chip.title = def.description || feature;
                    chip.innerHTML = `
                        <span class="flag-icon">${def.icon}</span>
                        <span>${def.label}</span>
                    `;
                    
                    // Add click event listener
                    chip.addEventListener('click', () => {
                        const isSelected = chip.dataset.selected === 'true';
                        chip.dataset.selected = (!isSelected).toString();
                        chip.classList.toggle('selected', !isSelected);
                        this.updateFlagFilter('spectral_features');
                    });
                    
                    this.spectralFeaturesContainer.appendChild(chip);
                } catch (error) {
                    console.warn(`Failed to get definition for spectral feature '${feature}':`, error);
                    // Fallback chip
                    const chip = document.createElement('button');
                    chip.className = 'flag-chip';
                    chip.style.background = '#e0e0e0';
                    chip.dataset.flag = feature;
                    chip.dataset.selected = 'false';
                    chip.title = feature;
                    chip.textContent = feature;
                    
                    chip.addEventListener('click', () => {
                        const isSelected = chip.dataset.selected === 'true';
                        chip.dataset.selected = (!isSelected).toString();
                        chip.classList.toggle('selected', !isSelected);
                        this.updateFlagFilter('spectral_features');
                    });
                    
                    this.spectralFeaturesContainer.appendChild(chip);
                }
            });
        }
    }
    
    updateFlagFilter(flagType) {
        let container, count;
        if (flagType === 'object_flags') {
            container = this.objectFlagsContainer;
            count = this.objectFlagsCount;
        } else if (flagType === 'dq_flags') {
            container = this.dqFlagsContainer;
            count = this.dqFlagsCount;
        } else if (flagType === 'spectral_features') {
            container = this.spectralFeaturesContainer;
            count = this.spectralFeaturesCount;
        }
        
        if (!container) return;
        
        const selected = Array.from(container.querySelectorAll('.flag-chip[data-selected="true"]'))
            .map(chip => chip.dataset.flag);
        
        // Update count display
        if (count) {
            if (selected.length > 0) {
                count.textContent = `(${selected.length})`;
                count.style.display = 'inline';
            } else {
                count.style.display = 'none';
            }
        }
        
        // Store in filter state
        if (!this.filterState) this.filterState = {};
        this.filterState[flagType] = selected;
        
        // Trigger filter update through base class
        this.applyFilters();
    }
    
    // Override getFilterState to include S/N range
    getFilterState() {
        const baseState = super.getFilterState();
        return {
            ...baseState,
            snrMin: this.snrRange.min,
            snrMax: this.snrRange.max
        };
    }
    
    applyNIRSpecFilters(filterState) {
        if (!window.nirspecTable) return;
        
        const table = window.nirspecTable;
        
        // Early exit if no filters are active
        const hasActiveFilters = filterState.search || 
            (filterState.observation && filterState.observation.length > 0) ||
            (filterState.field && filterState.field.length > 0) ||
            (filterState.grating && filterState.grating.length > 0) ||
            (filterState.quality && filterState.quality.length > 0) ||
            (filterState.program && filterState.program.length > 0) ||
            (filterState.object_flags && filterState.object_flags.length > 0) ||
            (filterState.dq_flags && filterState.dq_flags.length > 0) ||
            (filterState.spectral_features && filterState.spectral_features.length > 0) ||
            (this.snrDataRange && (filterState.snrMin > this.snrDataRange.min || filterState.snrMax < this.snrDataRange.max));
            
        if (!hasActiveFilters) {
            table.applyFilters(); // Show all data
            return;
        }

        // Create filter function for the table
        const filterFunction = (obj) => {
            // Search filter with caching
            if (filterState.search) {
                const searchLower = filterState.search.toLowerCase();
                
                // Cache searchable string for each object to avoid repeated processing
                if (!obj._searchCache) {
                    obj._searchCache = [
                        obj.id,
                        obj.observation,
                        obj.field,
                        obj.program,
                        ...(obj.gratings || [])
                    ].filter(Boolean).map(s => s.toLowerCase()).join(' ');
                }
                
                if (!obj._searchCache.includes(searchLower)) {
                    return false;
                }
            }
            
            // Multi-select filters
            if (filterState.observation && filterState.observation.length > 0 && 
                !filterState.observation.includes(obj.observation)) {
                return false;
            }
            
            if (filterState.field && filterState.field.length > 0 && 
                !filterState.field.includes(obj.field)) {
                return false;
            }
            
            if (filterState.grating && filterState.grating.length > 0) {
                const objGratings = obj.gratings || [];
                if (!filterState.grating.some(g => objGratings.includes(g))) {
                    return false;
                }
            }
            
            // Quality filter - using new 0-4 scale directly (no migration needed)
            if (filterState.quality && filterState.quality.length > 0) {
                const objectQuality = obj.redshift_quality;
                
                if (!filterState.quality.includes(String(objectQuality))) {
                    return false;
                }
            }
            
            if (filterState.program && filterState.program.length > 0 && 
                !filterState.program.includes(obj.program)) {
                return false;
            }
            
            // S/N range filter - only filter if different from data range
            if (this.snrDataRange && 
                (filterState.snrMin > this.snrDataRange.min || filterState.snrMax < this.snrDataRange.max)) {
                const snr = obj.max_snr;
                if (snr === null || snr === undefined || snr < filterState.snrMin || snr > filterState.snrMax) {
                    return false;
                }
            }
            
            // Object flags filter - handle both old string arrays and new bitmasks
            if (filterState.object_flags && filterState.object_flags.length > 0) {
                let objFlags = [];
                
                if (obj.collaborative) {
                    // Handle old string format
                    if (Array.isArray(obj.collaborative.object_flags)) {
                        objFlags = obj.collaborative.object_flags;
                    } else if (typeof obj.collaborative.obj_flags_bitmask === 'number' && window.flagManager?.initialized) {
                        // Handle new bitmask format
                        try {
                            objFlags = window.flagManager.decodeBitmask('object_flags', obj.collaborative.obj_flags_bitmask);
                        } catch (error) {
                            console.warn('Failed to decode object flags for filtering:', error);
                        }
                    }
                }
                
                if (!filterState.object_flags.some(flag => objFlags.includes(flag))) {
                    return false;
                }
            }
            
            // DQ flags filter - handle both old string arrays and new bitmasks
            if (filterState.dq_flags && filterState.dq_flags.length > 0) {
                let dqFlags = [];
                
                if (obj.collaborative) {
                    // Handle old string format
                    if (Array.isArray(obj.collaborative.dq_flags)) {
                        dqFlags = obj.collaborative.dq_flags;
                    } else if (typeof obj.collaborative.dq_flags_bitmask === 'number' && window.flagManager?.initialized) {
                        // Handle new bitmask format
                        try {
                            dqFlags = window.flagManager.decodeBitmask('dq_flags', obj.collaborative.dq_flags_bitmask);
                        } catch (error) {
                            console.warn('Failed to decode DQ flags for filtering:', error);
                        }
                    }
                }
                
                if (!filterState.dq_flags.some(flag => dqFlags.includes(flag))) {
                    return false;
                }
            }
            
            // Spectral features filter - handle bitmask format
            if (filterState.spectral_features && filterState.spectral_features.length > 0) {
                let spectralFeatures = [];
                
                if (obj.collaborative && typeof obj.collaborative.z_features === 'number' && window.flagManager?.initialized) {
                    try {
                        spectralFeatures = window.flagManager.decodeBitmask('spectral_features', obj.collaborative.z_features);
                    } catch (error) {
                        console.warn('Failed to decode spectral features for filtering:', error);
                    }
                }
                
                if (!filterState.spectral_features.some(feature => spectralFeatures.includes(feature))) {
                    return false;
                }
            }
            
            return true;
        };
        
        // Apply filters to table
        table.applyFilters(filterFunction);
    }

    clearNIRSpecFilters() {
        if (!window.nirspecTable) return;
        
        // Reset S/N range to data bounds
        if (this.snrDataRange) {
            this.snrRange = { ...this.snrDataRange };
            const minSlider = document.getElementById('snr-min-slider');
            const maxSlider = document.getElementById('snr-max-slider');
            if (minSlider && maxSlider) {
                minSlider.value = this.snrRange.min;
                maxSlider.value = this.snrRange.max;
                document.getElementById('snr-count').style.display = 'none';
            }
        }
        
        // Reset flag chips
        if (this.objectFlagsContainer) {
            this.objectFlagsContainer.querySelectorAll('.flag-chip').forEach(chip => {
                chip.dataset.selected = 'false';
                chip.classList.remove('selected');
            });
            if (this.objectFlagsCount) {
                this.objectFlagsCount.style.display = 'none';
            }
        }
        
        if (this.dqFlagsContainer) {
            this.dqFlagsContainer.querySelectorAll('.flag-chip').forEach(chip => {
                chip.dataset.selected = 'false';
                chip.classList.remove('selected');
            });
            if (this.dqFlagsCount) {
                this.dqFlagsCount.style.display = 'none';
            }
        }
        
        if (this.spectralFeaturesContainer) {
            this.spectralFeaturesContainer.querySelectorAll('.flag-chip').forEach(chip => {
                chip.dataset.selected = 'false';
                chip.classList.remove('selected');
            });
            if (this.spectralFeaturesCount) {
                this.spectralFeaturesCount.style.display = 'none';
            }
        }
        
        // Clear filters on table
        window.nirspecTable.applyFilters();
    }
    
    // updateFilterFromCheckboxes, updateFilterCount, and clearFilters are now inherited from BaseFilters
    
    downloadFilteredData() {
        if (!window.nirspecTable) return;
        
        const filteredData = window.nirspecTable.getFilteredData();
        
        if (filteredData.length === 0) {
            alert('No data to download with current filters');
            return;
        }

        // Convert NIRSpec objects to file format expected by DownloadGenerator
        const files = this.convertToFileList(filteredData);
        
        try {
            const script = this.downloadGenerator.generateScript(files, {
                field: 'nirspec',
                total_spectra: filteredData.length
            });
            
            // Download the script
            this.downloadGenerator.downloadAsFile(script, `nirspec_download_${Date.now()}.sh`);
            
        } catch (error) {
            console.error('Failed to generate download script:', error);
            alert('Failed to generate download script. Please try again.');
        }
    }

    convertToFileList(nirspecObjects) {
        // Convert NIRSpec objects to file list format
        const files = [];
        
        nirspecObjects.forEach(obj => {
            // Parse object ID to get observation and object parts
            const parts = obj.id.split('_');
            const observation = parts.slice(0, -1).join('_');
            const msaId = parts[parts.length - 1];
            
            // Add files for each grating
            if (obj.gratings && obj.gratings.length > 0) {
                obj.gratings.forEach(grating => {
                    // Add spectrum file
                    files.push({
                        filename: `${obj.id}_${grating}_spec.fits`,
                        path: `data/nirspec/${observation}/${msaId}/${obj.id}_${grating}_spec.fits`,
                        size: 'unknown',
                        sizeBytes: 0
                    });
                    
                    // Add 2D spectrum file
                    files.push({
                        filename: `${obj.id}_${grating}_2d.fits`,
                        path: `data/nirspec/${observation}/${msaId}/${obj.id}_${grating}_2d.fits`,
                        size: 'unknown', 
                        sizeBytes: 0
                    });
                });
            }
            
            // Add zfit files (per-object)
            files.push({
                filename: `${obj.id}_zfit.fits`,
                path: `data/nirspec/${observation}/${msaId}/${obj.id}_zfit.fits`,
                size: 'unknown',
                sizeBytes: 0
            });
        });
        
        return files;
    }
    
    // getState, setState, and updateUIFromState are now inherited from BaseFilters

}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.nirspecFilters = new NIRSpecFilters();
});