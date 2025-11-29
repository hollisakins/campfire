/**
 * NIRSpec object detail page functionality
 * Matches the design from cw_nirspec_example.png
 */

// Flag definitions with colors and descriptions
const FLAG_DEFINITIONS = {
    object_flags: {
        'LRD': { 
            color: '#ffcccb',  // Pastel red for Little Red Dots
            label: 'LRD', 
            icon: '🔴',
            description: 'Little Red Dot'
        },
        'Broad line': { 
            color: '#c8e6c9',  // Pastel green for emission features
            label: 'Broad Line', 
            icon: '🌋',
            description: 'Broad emission line detected'
        },
        'Lya': { 
            color: '#bbdefb',  // Pastel blue for Lyman-alpha
            label: 'Lyα', 
            icon: '✨',
            description: 'Lyman-alpha emission'
        },
        'Balmer break': { 
            color: '#e1bee7',  // Pastel purple for continuum features
            label: 'Balmer Break', 
            icon: '📈',
            description: 'Balmer break detected'
        }
    },
    dq_flags: {
        'Chip gap / detector gap': { 
            color: '#fff9c4',  // Pastel yellow for warnings
            label: 'Chip Gap', 
            icon: '⚠️',
            description: 'Chip or detector gap affecting spectrum'
        },
        'Contamination from open shutter': { 
            color: '#ffe0b2',  // Pastel orange for contamination
            label: 'Contamination', 
            icon: '🚫',
            description: 'Open shutter contamination'
        },
        'Possible stuck closed shutter': { 
            color: '#ffcccb',  // Pastel red for serious issues
            label: 'Stuck Shutter', 
            icon: '🔒',
            description: 'Possible stuck closed shutter'
        },
        'Multiple sources in slitlet': { 
            color: '#b3e5fc',  // Pastel cyan for blending
            label: 'Multiple Sources', 
            icon: '👥',
            description: 'Multiple sources in slitlet'
        },
        'No detection': { 
            color: '#e0e0e0',  // Pastel gray for non-detection
            label: 'No Detection', 
            icon: '❌',
            description: 'No source detected'
        },
        'Spectral overlap in grating': { 
            color: '#f3e5f5',  // Pastel purple for overlap issues
            label: 'Spectral Overlap', 
            icon: '🔗',
            description: 'Spectral overlap between gratings affecting data quality'
        }
    }
};

class NIRSpecObject {
    constructor() {
        this.objectId = this.getObjectIdFromURL();
        this.navigationContext = this.getNavigationContext();
        this.objectData = null;
        this.masterData = null;
        this.programsData = null;
        this.activeTab = null;
        this.currentVersion = null;   // Track current version being displayed
        
        // Navigation state
        this.navigationObjects = [];  // Filtered/sorted object list
        this.currentIndex = -1;       // Current object position
        this.totalCount = 0;          // Total objects in context
        this.hasValidContext = false; // Whether navigation context is usable
        
        this.init();
    }
    
    async init() {
        try {
            // Initialize breadcrumb manager for object detail page
            if (window.BreadcrumbManager && !window.breadcrumbManager) {
                window.breadcrumbManager = new window.BreadcrumbManager({
                    showHome: true
                });
                window.breadcrumbManager.init();
            }
            
            // Load data
            const manifest = await window.dataLoader.loadNIRSpecManifest();
            const programs = await window.dataLoader.loadNIRSpecPrograms();
            this.masterData = manifest;
            this.programsData = programs;
            
            // Find object data
            this.objectData = manifest.objects.find(obj => obj.id === this.objectId);
            
            if (!this.objectData) {
                this.showError(`Object ${this.objectId} not found`);
                return;
            }
            
            // Initialize version support
            this.initializeVersioning();
            
            // Initialize navigation context if available
            this.initializeNavigation();
            
            // Render page components
            this.renderHeader();
            this.renderTabs();
            this.setupEventListeners();
            
            // Set initial active tab (first grating or redshift)
            const versionData = this.getCurrentVersionData();
            const firstGrating = versionData.gratings?.[0];
            this.setActiveTab(firstGrating || 'redshift');
            
        } catch (error) {
            console.error('Failed to load object data:', error);
            this.showError('Failed to load object data');
        }
    }
    
    getObjectIdFromURL() {
        const params = new URLSearchParams(window.location.search);
        return params.get('id');
    }
    
    getVersionFromURL() {
        const params = new URLSearchParams(window.location.search);
        return params.get('version');
    }
    
    initializeVersioning() {
        // Set current version from URL parameter or default to current_version
        const urlVersion = this.getVersionFromURL();
        
        if (urlVersion && this.objectData.available_versions && this.objectData.available_versions.includes(urlVersion)) {
            this.currentVersion = urlVersion;
        } else {
            this.currentVersion = this.objectData.current_version || 'v0.1';
        }
        
        console.log(`Initialized version: ${this.currentVersion}`);
        
        // If object has versions but current version is not available, show error
        if (this.objectData.available_versions && this.objectData.available_versions.length > 0) {
            if (!this.objectData.available_versions.includes(this.currentVersion)) {
                console.warn(`Version ${this.currentVersion} not available, using ${this.objectData.current_version}`);
                this.currentVersion = this.objectData.current_version;
            }
        }
    }
    
    getNavigationContext() {
        const params = new URLSearchParams(window.location.search);
        const contextParam = params.get('context');
        // console.log('Raw context param:', contextParam);
        
        if (contextParam) {
            try {
                const context = JSON.parse(atob(contextParam));
                // console.log('Parsed navigation context:', context);
                return context;
            } catch (e) {
                console.warn('Failed to parse navigation context:', e);
                return null;
            }
        }
        // console.log('No context parameter found');
        return null;
    }
    
    /**
     * Initialize navigation functionality using the provided context
     */
    initializeNavigation() {
        if (!this.navigationContext || !this.masterData) {
            console.log('No navigation context available');
            this.hasValidContext = false;
            return;
        }
        
        try {
            // Validate context structure
            if (!this.validateNavigationContext(this.navigationContext)) {
                console.warn('Invalid navigation context structure');
                this.hasValidContext = false;
                return;
            }
            
            console.log('Initializing navigation with context:', this.navigationContext);
            
            // Recreate the filtered dataset from the table context
            this.recreateFilteredDataset();
            
            // Apply sorting from context
            this.applySortingFromContext();
            
            // Find current object position
            this.findCurrentObjectIndex();
            
            this.hasValidContext = true;
            console.log(`Navigation initialized: object ${this.currentIndex + 1} of ${this.totalCount}`);
            
        } catch (error) {
            console.error('Failed to initialize navigation:', error);
            this.hasValidContext = false;
        }
    }
    
    /**
     * Validate that the navigation context has the required structure
     */
    validateNavigationContext(context) {
        try {
            return context && 
                   typeof context === 'object' &&
                   context.filters &&
                   typeof context.filters === 'object' &&
                   context.sort &&
                   typeof context.sort === 'object' &&
                   typeof context.sort.column === 'string' &&
                   typeof context.sort.direction === 'string' &&
                   ['asc', 'desc'].includes(context.sort.direction);
        } catch (error) {
            console.error('Error validating navigation context:', error);
            return false;
        }
    }
    
    /**
     * Recreate the filtered dataset using the context filters
     * Replicates the logic from nirspec_filters.js
     */
    recreateFilteredDataset() {
        try {
            if (!this.masterData || !this.navigationContext.filters) {
                this.navigationObjects = [...(this.masterData?.objects || [])];
                return;
            }
            
            const filterState = this.navigationContext.filters;
            const allObjects = this.masterData.objects || [];
        
        // Apply the same filter logic as nirspec_filters.js
        this.navigationObjects = allObjects.filter(obj => {
            // Search filter
            if (filterState.search) {
                const searchLower = filterState.search.toLowerCase();
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
            
            if (filterState.quality && filterState.quality.length > 0 && 
                !filterState.quality.includes(String(obj.redshift_quality))) {
                return false;
            }
            
            return true;
            });
            
            console.log(`Filtered dataset: ${this.navigationObjects.length} objects from ${allObjects.length} total`);
        } catch (error) {
            console.error('Error recreating filtered dataset:', error);
            // Fallback to all objects if filtering fails
            this.navigationObjects = [...(this.masterData?.objects || [])];
        }
    }
    
    /**
     * Apply sorting to the filtered dataset using context sort parameters
     * Replicates the logic from base_table.js
     */
    applySortingFromContext() {
        try {
            if (!this.navigationContext.sort || !this.navigationContext.sort.column) {
                return;
            }
            
            const sortColumn = this.navigationContext.sort.column;
            const sortDirection = this.navigationContext.sort.direction;
            
            // Skip sorting if it's the default 'id' column and sortColumn was originally null
            // This means no explicit sorting was applied in the table
            if (sortColumn === 'id' && this.navigationContext.sort.direction === 'asc') {
                // Check if this was explicitly set or just the default
                // For now, let's apply the sorting anyway to maintain consistency
            }
        
        this.navigationObjects.sort((a, b) => {
            let aVal = a[sortColumn];
            let bVal = b[sortColumn];
            
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
            
            return sortDirection === 'asc' ? result : -result;
            });
            
            console.log(`Applied sorting: ${sortColumn} ${sortDirection}`);
        } catch (error) {
            console.error('Error applying sorting:', error);
            // Continue without sorting if it fails
        }
    }
    
    /**
     * Find the current object's position in the filtered/sorted dataset
     */
    findCurrentObjectIndex() {
        this.totalCount = this.navigationObjects.length;
        this.currentIndex = this.navigationObjects.findIndex(obj => obj.id === this.objectId);
        
        if (this.currentIndex === -1) {
            console.warn(`Current object ${this.objectId} not found in filtered dataset`);
            // Object was filtered out - disable navigation
            this.hasValidContext = false;
        }
        
        console.log(`Object position: ${this.currentIndex + 1} of ${this.totalCount}`);
    }
    
    renderHeader() {
        // Update main title with object ID in monospace (no "Object" prefix)
        const objectTitle = document.getElementById('objectTitle');
        if (objectTitle) {
            objectTitle.textContent = this.objectData.id;
            objectTitle.style.fontFamily = 'var(--font-family-mono)';
        }
        
        // Update subtitle with new metadata format:
        // RA, Dec: 34.429616, -5.11232 (UDS)
        // CAPERS (GO#6368; P.I. M. Dickinson)
        const objectSubtitle = document.getElementById('objectSubtitle');
        if (objectSubtitle) {
            const lines = [];
            
            // Line 1: RA, Dec: X, Y (Field)
            if (this.objectData.ra && this.objectData.dec) {
                const ra = this.objectData.ra.toFixed(6);
                const dec = this.objectData.dec.toFixed(5);
                lines.push(`RA, Dec: ${ra}, ${dec} (${this.objectData.field})`);
            }
            
            // Line 2: PROGRAM (TYPE#ID; P.I. Name)
            if (this.objectData.program && this.objectData.program_id) {
                const programInfo = this.getProgramInfo(this.objectData.program);
                const programType = programInfo && programInfo.program_type ? programInfo.program_type : 'GO';
                const piInfo = programInfo && programInfo.pi ? `; P.I. ${programInfo.pi}` : '';
                lines.push(`${this.objectData.program} (${programType}#${this.objectData.program_id}${piInfo})`);
            }
            
            objectSubtitle.innerHTML = lines.join('<br>');
            objectSubtitle.style.fontFamily = 'var(--font-family-mono)';
        }
        
        // Add version selector if multiple versions are available
        this.renderVersionSelector();
        
        // Update unified breadcrumb with dynamic object name
        if (window.breadcrumbManager) {
            // Use full object ID and remove coordinates from breadcrumb
            window.breadcrumbManager.setDynamicTitle(this.objectData.id);
        }
        
        // Update metrics badges
        this.updateMetricsBadges();
        
        // Update navigation
        this.updateNavigation();
    }
    
    renderVersionSelector() {
        // Check if multiple versions are available
        if (!this.objectData.available_versions || this.objectData.available_versions.length <= 1) {
            // Hide version selector if it exists
            const existingSelector = document.getElementById('versionSelectorContainer');
            if (existingSelector) {
                existingSelector.style.display = 'none';
            }
            return;
        }
        
        // Find or create version selector container
        let container = document.getElementById('versionSelectorContainer');
        if (!container) {
            // Create container and insert it after the subtitle
            container = document.createElement('div');
            container.id = 'versionSelectorContainer';
            container.className = 'version-selector-container';
            
            const subtitle = document.getElementById('objectSubtitle');
            if (subtitle && subtitle.parentNode) {
                subtitle.parentNode.insertBefore(container, subtitle.nextSibling);
            }
        }
        
        // Get version metadata for current version
        const currentVersionData = this.getCurrentVersionData();
        const versionMeta = currentVersionData?.version_metadata || {};
        
        // Format version date
        const versionDate = versionMeta.pipeline_date ? 
            new Date(versionMeta.pipeline_date).toLocaleDateString() : 
            '';
        
        // Generate version selector HTML
        container.innerHTML = `
            <div class="version-selector-wrapper">
                <label for="versionSelect" class="version-label">Pipeline Version:</label>
                <select id="versionSelect" class="version-dropdown">
                    ${this.objectData.available_versions.map(version => `
                        <option value="${version}" ${version === this.currentVersion ? 'selected' : ''}>
                            ${version}${version === this.objectData.current_version ? ' (Latest)' : ''}
                        </option>
                    `).join('')}
                </select>
                <span class="version-info">
                    ${versionDate ? `Processed: ${versionDate}` : ''}
                    ${versionMeta.pipeline_version ? ` • Pipeline ${versionMeta.pipeline_version}` : ''}
                </span>
            </div>
        `;
        
        // Add event listener for version changes
        const select = container.querySelector('#versionSelect');
        if (select) {
            select.addEventListener('change', (e) => {
                this.handleVersionChange(e.target.value);
            });
        }
        
        container.style.display = 'block';
    }
    
    getCurrentVersionData() {
        // If object has versions, return the current version's data
        if (this.objectData.versions && this.currentVersion) {
            return this.objectData.versions[this.currentVersion];
        }
        
        // Fall back to top-level data for backward compatibility
        return this.objectData;
    }
    
    handleVersionChange(newVersion) {
        if (newVersion === this.currentVersion) {
            return; // No change needed
        }
        
        console.log(`Switching from version ${this.currentVersion} to ${newVersion}`);
        this.currentVersion = newVersion;
        
        // Update URL without reload
        const url = new URL(window.location);
        url.searchParams.set('version', newVersion);
        window.history.replaceState({}, '', url);
        
        // Re-render all components with new version data
        this.renderVersionSelector(); // Update version info display
        this.updateMetricsBadges();   // Update badges with version-specific data
        this.renderTabs();            // Regenerate all tabs with new version data
        
        // Reactivate the current tab
        if (this.activeTab) {
            this.setActiveTab(this.activeTab);
        }
    }
    
    /**
     * Get program information from programs data
     */
    getProgramInfo(programName) {
        if (!this.programsData) {
            return null;
        }
        
        // The TOML parser creates flat keys like "programs.capers" instead of nested objects
        // Find program by looking for keys that start with "programs." and have a name property
        const programKeys = Object.keys(this.programsData)
            .filter(key => key.startsWith('programs.') && !key.includes('.observations'));
        
        const matchingProgramKey = programKeys
            .find(key => {
                const program = this.programsData[key];
                return program && program.name && program.name.toLowerCase() === programName.toLowerCase();
            });
        
        return matchingProgramKey ? this.programsData[matchingProgramKey] : null;
    }
    
    /**
     * Render grating details with real metadata
     */
    renderGratingDetails(grating, gratingFiles) {
        const metadata = gratingFiles.metadata || {};
        
        const details = [];
        
        // Grating and Filter info
        if (metadata.grating && metadata.filter) {
            details.push({
                label: 'Configuration:',
                value: `${metadata.grating}/${metadata.filter}`,
                highlight: false
            });
        }
        
        // Exposure time
        if (metadata.exptime) {
            const expTime = parseFloat(metadata.exptime);
            const expTimeFormatted = expTime >= 3600 ? 
                `${(expTime / 3600).toFixed(1)} hours` : 
                expTime >= 60 ? 
                `${(expTime / 60).toFixed(1)} minutes` : 
                `${expTime.toFixed(0)} seconds`;
                
            details.push({
                label: 'Exposure Time:',
                value: expTimeFormatted,
                highlight: false
            });
        }
        
        // Number of combined exposures
        if (metadata.ncombine) {
            details.push({
                label: '# Combined:',
                value: `${metadata.ncombine} exposures`,
                highlight: false
            });
        }
        
        // Maximum S/N ratio
        if (metadata.max_snr) {
            details.push({
                label: 'Max S/N:',
                value: parseFloat(metadata.max_snr).toFixed(1),
                highlight: true
            });
        }
        
        // Placeholder for other spectral quality metrics
        // For now, showing placeholder values that would be computed from the actual spectrum
        details.push({
            label: 'Spectral Coverage:',
            value: grating.toLowerCase().includes('prism') ? '0.6 - 5.3 μm' : 'TBD',
            highlight: false
        });
        
        // Generate the HTML
        return details.map(detail => `
            <div class="detail-item">
                <span class="detail-label">${detail.label}</span>
                <span class="detail-value ${detail.highlight ? 'highlight' : ''}">${detail.value}</span>
            </div>
        `).join('');
    }
    
    updateMetricsBadges() {
        // Get version-specific data
        const versionData = this.getCurrentVersionData();
        
        // Max S/N - compute maximum from all grating metadata
        const metricMaxSN = document.getElementById('metricMaxSN');
        if (metricMaxSN) {
            let maxSNR = null;
            
            // Iterate through all gratings to find maximum S/N
            if (versionData.gratings) {
                for (const grating of versionData.gratings) {
                    const gratingFiles = versionData.files[grating];
                    if (gratingFiles && gratingFiles.metadata && gratingFiles.metadata.max_snr) {
                        const snr = parseFloat(gratingFiles.metadata.max_snr);
                        if (!isNaN(snr)) {
                            maxSNR = maxSNR === null ? snr : Math.max(maxSNR, snr);
                        }
                    }
                }
            }
            
            metricMaxSN.textContent = maxSNR !== null ? maxSNR.toFixed(1) : '-';
        }
        
        // Redshift
        const metricRedshift = document.getElementById('metricRedshift');
        if (metricRedshift) {
            metricRedshift.textContent = versionData.redshift?.toFixed(2) || '-';
        }
        
        // Quality
        const metricQuality = document.getElementById('metricQuality');
        const qualityBadge = document.getElementById('qualityBadge');
        if (metricQuality && qualityBadge) {
            const quality = versionData.redshift_quality;
            
            // Try to use FlagManager first for consistent labeling
            let qualityLabel = '-';
            let qualityClass = '';
            
            // Define CSS class mapping that matches existing styles
            const qualityClasses = {'0': 'quality-none', '1': 'quality-bad', '2': 'quality-tentative', '3': 'quality-probable', '4': 'quality-good'};
            
            if (window.flagManager?.initialized) {
                try {
                    const qualityInfo = window.flagManager.getQualityInfo(quality);
                    qualityLabel = qualityInfo.short;
                    qualityClass = qualityClasses[String(quality)] || '';
                } catch (error) {
                    console.warn('Failed to get quality info from FlagManager:', error);
                }
            }
            
            // Fallback to hardcoded short names from config if FlagManager not available
            if (qualityLabel === '-') {
                const qualityLabels = {'0': 'None', '1': 'Bad', '2': 'Tent.', '3': 'Prob.', '4': 'Secure'};
                
                qualityLabel = qualityLabels[String(quality)] || '-';
                qualityClass = qualityClasses[String(quality)] || '';
            }
            
            metricQuality.textContent = qualityLabel;
            
            // Reset classes and add appropriate one
            qualityBadge.className = 'metric-badge ' + qualityClass;
        }
        
        // Gratings count
        const metricGratings = document.getElementById('metricGratings');
        if (metricGratings) {
            metricGratings.textContent = this.objectData.gratings?.length || '0';
        }
    }
    
    updateNavigation() {
        // Update navigation info display
        const navigationInfo = document.getElementById('navigationInfo');
        if (navigationInfo) {
            if (this.hasValidContext && this.totalCount > 0) {
                navigationInfo.textContent = `${this.currentIndex + 1} of ${this.totalCount}`;
            } else {
                navigationInfo.textContent = '1 of 1';
            }
        }
        
        // Update navigation button states
        this.updateNavigationButtons();
    }
    
    /**
     * Update the enabled/disabled state of navigation buttons
     */
    updateNavigationButtons() {
        const prevBtn = document.getElementById('prevObjectBtn');
        const nextBtn = document.getElementById('nextObjectBtn');
        
        if (prevBtn) {
            const canGoPrevious = this.hasValidContext && this.currentIndex > 0;
            prevBtn.disabled = !canGoPrevious;
            prevBtn.classList.toggle('disabled', !canGoPrevious);
        }
        
        if (nextBtn) {
            const canGoNext = this.hasValidContext && this.currentIndex < this.totalCount - 1;
            nextBtn.disabled = !canGoNext;
            nextBtn.classList.toggle('disabled', !canGoNext);
        }
    }
    
    renderTabs() {
        const tabNavList = document.getElementById('tabNavList');
        const tabContent = document.getElementById('tabContent');
        
        if (!tabNavList || !tabContent) return;
        
        // Clear existing content
        tabNavList.innerHTML = '';
        tabContent.innerHTML = '';
        
        // Get version-specific data
        const versionData = this.getCurrentVersionData();
        
        // Create grating tabs
        if (versionData.gratings && versionData.gratings.length > 0) {
            versionData.gratings.forEach(grating => {
                this.createGratingTab(grating, tabNavList, tabContent);
            });
        }
        
        // Create standard tabs
        this.createStandardTab('redshift', 'Redshift', tabNavList, tabContent);
        this.createStandardTab('photometry', 'Photometry', tabNavList, tabContent);
        this.createStandardTab('notes', 'Notes', tabNavList, tabContent);
        this.createStandardTab('context', 'Context', tabNavList, tabContent);
    }
    
    createGratingTab(grating, tabNavList, tabContent) {
        // Create tab button
        const tabItem = document.createElement('li');
        tabItem.className = 'tab-nav-item';
        
        const tabButton = document.createElement('button');
        tabButton.className = 'tab-nav-button';
        tabButton.dataset.tab = grating;
        tabButton.textContent = grating.toUpperCase();
        
        tabButton.addEventListener('click', () => {
            this.setActiveTab(grating);
        });
        
        tabItem.appendChild(tabButton);
        tabNavList.appendChild(tabItem);
        
        // Create tab content pane
        const tabPane = document.createElement('div');
        tabPane.className = 'tab-pane';
        tabPane.id = `${grating}-tab`;
        
        tabPane.innerHTML = this.generateGratingContent(grating);
        tabContent.appendChild(tabPane);
    }
    
    createStandardTab(tabId, tabLabel, tabNavList, tabContent) {
        // Create tab button
        const tabItem = document.createElement('li');
        tabItem.className = 'tab-nav-item';
        
        const tabButton = document.createElement('button');
        tabButton.className = 'tab-nav-button';
        tabButton.dataset.tab = tabId;
        tabButton.textContent = tabLabel;
        
        tabButton.addEventListener('click', () => {
            this.setActiveTab(tabId);
        });
        
        tabItem.appendChild(tabButton);
        tabNavList.appendChild(tabItem);
        
        // Create tab content pane
        const tabPane = document.createElement('div');
        tabPane.className = 'tab-pane';
        tabPane.id = `${tabId}-tab`;
        
        tabPane.innerHTML = this.generateStandardTabContent(tabId);
        tabContent.appendChild(tabPane);
    }
    
    generateGratingContent(grating) {
        const versionData = this.getCurrentVersionData();
        const gratingFiles = versionData.files?.[grating] || {};
        
        return `
            <div class="grating-content">
                <div class="grating-header">
                    <h2 class="grating-title">${grating.toUpperCase()} Spectroscopy</h2>
                </div>
                
                <!-- Grating Details Section -->
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">Grating Details</h3>
                    </div>
                    <div class="section-content">
                        <div class="grating-details-grid">
                            ${this.renderGratingDetails(grating, gratingFiles)}
                        </div>
                        <div class="section-actions">
                            ${gratingFiles.spec ? `<a href="data/nirspec/${gratingFiles.spec}" class="btn-download" download>📊 Download FITS File</a>` : ''}
                        </div>
                    </div>
                </div>
                
                <!-- 2D Spectrum Section -->
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">📊 2D Spectrum</h3>
                    </div>
                    <div class="section-content">
                        <div class="plot-container">
                            <div class="plot-content">
                                ${gratingFiles.spec_plot ? 
                                    `<img src="data/nirspec/${gratingFiles.spec_plot}" alt="${grating} 2D spectrum" style="max-width: 100%; height: auto; display: block;">` : 
                                    'Plot will be generated when available'
                                }
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }
    
    generateStandardTabContent(tabId) {
        switch (tabId) {
            case 'redshift':
                return this.generateRedshiftContent();
            case 'photometry':
                return this.generatePhotometryContent();
            case 'notes':
                return this.generateNotesContent();
            case 'context':
                return this.generateContextContent();
            default:
                return '<p>Tab content not implemented yet.</p>';
        }
    }
    
    generateRedshiftContent() {
        const zfitFiles = this.objectData.files || {};
        const collaborative = this.objectData.collaborative || {};
        
        return `
            <div class="analysis-content">
                <h2>Redshift Analysis</h2>
                
                <!-- Inspection Results Section -->
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">👁️ Inspection Results</h3>
                    </div>
                    <div class="section-content">
                        <div class="grating-details-grid">
                            <div class="detail-item">
                                ${(() => {
                                    const hasInspections = (collaborative.total_submissions || 0) > 0;
                                    const redshiftLabel = hasInspections ? 'Inspected Redshift:' : 'Redshift:';
                                    const redshiftValue = hasInspections ? 
                                        (collaborative.consensus_redshift?.toFixed(4) || 'Unknown') : 
                                        (this.objectData.redshift?.toFixed(4) || '-');
                                    return `
                                        <span class="detail-label">${redshiftLabel}</span>
                                        <span class="detail-value highlight">${redshiftValue}</span>
                                    `;
                                })()}
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Quality:</span>
                                <span class="detail-value">${this.getConsensusQualityLabel(collaborative.consensus_redshift_quality)}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Inspections:</span>
                                <span class="detail-value">${collaborative.total_submissions || 0}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Last Updated:</span>
                                <span class="detail-value">${collaborative.last_updated ? new Date(collaborative.last_updated).toLocaleDateString() : 'Never'}</span>
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Automated Results Section -->
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">🤖 Automated Results</h3>
                    </div>
                    <div class="section-content">
                        <div class="grating-details-grid">
                            <div class="detail-item">
                                <span class="detail-label">Auto Redshift:</span>
                                <span class="detail-value highlight">${this.objectData.auto_redshift?.toFixed(4) || '-'}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">χ²:</span>
                                <span class="detail-value">${this.objectData.chi2_min ? this.objectData.chi2_min.toFixed(1) : '-'}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Confidence:</span>
                                <span class="detail-value">${this.objectData.redshift_confidence ? this.objectData.redshift_confidence.toFixed(0) + '%' : '-'}</span>
                            </div>
                        </div>
                        <div class="section-actions">
                            ${zfitFiles.zfit ? `<a href="data/nirspec/${zfitFiles.zfit}" class="btn-download" download>📊 Download Results</a>` : ''}
                        </div>
                    </div>
                </div>
                
                <!-- Redshift Fitting Plot Section -->
                ${zfitFiles.zfit_plot ? `
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">📈 Redshift Fitting</h3>
                    </div>
                    <div class="section-content">
                        <div class="plot-container">
                            <div class="plot-content">
                                <img src="data/nirspec/${zfitFiles.zfit_plot}" alt="Redshift fitting plot" style="max-width: 100%; height: auto; display: block;">
                            </div>
                        </div>
                    </div>
                </div>
                ` : ''}
            </div>
        `;
    }
    
    generatePhotometryContent() {
        return `
            <div class="photometry-content">
                <h2>NIRCam Photometry</h2>
                <div class="plot-content" style="text-align: center; padding: 2rem; color: #666;">
                    <p>NIRCam photometry integration coming soon...</p>
                </div>
            </div>
        `;
    }
    
    generateNotesContent() {
        // Get collaborative data with safe defaults
        const collaborative = this.objectData.collaborative || {};
        const consensusRedshift = collaborative.consensus_redshift;
        const consensusQuality = collaborative.consensus_redshift_quality;
        // Handle both old string array and new bitmask formats for flags
        const spectralFeatures = collaborative.z_features !== undefined ? collaborative.z_features : (collaborative.spectral_features || []);
        const objectFlags = collaborative.obj_flags_bitmask !== undefined ? collaborative.obj_flags_bitmask : (collaborative.object_flags || []);
        const dqFlags = collaborative.dq_flags_bitmask !== undefined ? collaborative.dq_flags_bitmask : (collaborative.dq_flags || []);
        const publications = collaborative.publications || [];
        const notesHistory = collaborative.notes_history || [];
        
        return `
            <div class="notes-content">
                <h2>Collaborative Notes & Redshift Inspection</h2>
                
                <!-- Inspection Summary Section -->
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">📊 Inspection Summary</h3>
                    </div>
                    <div class="section-content">
                        <!-- Key metrics in grid layout -->
                        <div class="grating-details-grid">
                            <div class="detail-item">
                                ${(() => {
                                    const hasInspections = (collaborative.total_submissions || 0) > 0;
                                    const redshiftLabel = hasInspections ? 'Inspected Redshift:' : 'Redshift:';
                                    const redshiftValue = hasInspections ? 
                                        (consensusRedshift ? consensusRedshift.toFixed(4) : 'Unknown') : 
                                        (this.objectData.redshift?.toFixed(4) || 'Not determined');
                                    return `
                                        <span class="detail-label">${redshiftLabel}</span>
                                        <span class="detail-value highlight">${redshiftValue}</span>
                                    `;
                                })()}
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Quality:</span>
                                <span class="detail-value">${this.getConsensusQualityLabel(consensusQuality)}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Total Inspections:</span>
                                <span class="detail-value">${collaborative.total_submissions || 0}</span>
                            </div>
                        </div>
                        
                        <!-- Full-width sections for multi-value items (conditionally rendered) -->
                        ${spectralFeatures && (Array.isArray(spectralFeatures) ? spectralFeatures.length > 0 : spectralFeatures > 0) ? `
                        <div class="detail-item-full-width">
                            <span class="detail-label">Spectral Features:</span>
                            <div class="detail-value">${this.formatFlags(spectralFeatures, 'spectral_features')}</div>
                        </div>
                        ` : ''}
                        
                        ${objectFlags && (Array.isArray(objectFlags) ? objectFlags.length > 0 : objectFlags > 0) ? `
                        <div class="detail-item-full-width">
                            <span class="detail-label">Object Features:</span>
                            <div class="detail-value">${this.formatFlags(objectFlags, 'object_flags')}</div>
                        </div>
                        ` : ''}
                        
                        ${dqFlags && (Array.isArray(dqFlags) ? dqFlags.length > 0 : dqFlags > 0) ? `
                        <div class="detail-item-full-width">
                            <span class="detail-label">Data Quality Flags:</span>
                            <div class="detail-value">${this.formatFlags(dqFlags, 'dq_flags')}</div>
                        </div>
                        ` : ''}
                        
                        ${publications && publications.length > 0 ? `
                        <div class="detail-item-full-width">
                            <span class="detail-label">Publications:</span>
                            <div class="detail-value">${this.formatPublications(publications)}</div>
                        </div>
                        ` : ''}
                    </div>
                </div>
                
                <!-- Notes History Section -->
                <div class="section-collapse expanded">
                    <div class="section-header">
                        <span class="section-toggle">▶</span>
                        <h3 class="section-title">📝 Notes History</h3>
                    </div>
                    <div class="section-content">
                        ${this.formatNotesHistory(notesHistory)}
                    </div>
                </div>
            </div>
        `;
    }
    
    generateContextContent() {
        return `
            <div class="context-content">
                <h2>Nearby Objects</h2>
                <div class="plot-content" style="text-align: center; padding: 2rem; color: #666;">
                    <p>Context view coming soon...</p>
                </div>
            </div>
        `;
    }
    
    getQualityLabel(quality) {
        const qualityLabels = {'-1': 'BAD', '0': 'NONE', '1': 'AUTO', '2': 'GOOD'};
        return qualityLabels[String(quality)] || '-';
    }
    
    getQualityLabelWithFlag(quality) {
        const qualityLabels = {'-1': 'BAD', '0': 'NONE', '1': 'AUTO', '2': 'GOOD'};
        const label = qualityLabels[String(quality)] || '-';
        return quality !== undefined && quality !== null ? `${label} (${quality})` : '-';
    }
    
    setActiveTab(tabId) {
        if (!tabId) return;
        
        this.activeTab = tabId;
        
        // Update tab buttons
        const tabButtons = document.querySelectorAll('.tab-nav-button');
        tabButtons.forEach(button => {
            if (button.dataset.tab === tabId) {
                button.classList.add('active');
            } else {
                button.classList.remove('active');
            }
        });
        
        // Update tab panes
        const tabPanes = document.querySelectorAll('.tab-pane');
        tabPanes.forEach(pane => {
            if (pane.id === `${tabId}-tab`) {
                pane.classList.add('active');
            } else {
                pane.classList.remove('active');
            }
        });
        
        // Initialize collapsible sections if needed
        this.initializeCollapsibleSections();
    }
    
    initializeCollapsibleSections() {
        // Add click handlers to section headers for collapsing/expanding
        const sectionHeaders = document.querySelectorAll('.section-header');
        sectionHeaders.forEach(header => {
            // Remove existing listeners to avoid duplicates
            header.replaceWith(header.cloneNode(true));
        });
        
        // Re-query after cloning
        const newSectionHeaders = document.querySelectorAll('.section-header');
        newSectionHeaders.forEach(header => {
            header.addEventListener('click', (e) => {
                const section = header.closest('.section-collapse');
                if (section) {
                    section.classList.toggle('expanded');
                }
            });
        });
    }
    
    setupEventListeners() {
        // Download All button
        const downloadAllBtn = document.getElementById('downloadAllBtn');
        if (downloadAllBtn) {
            downloadAllBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.handleDownloadAll();
            });
        }
        
        // Inspection Form button
        const inspectionFormBtn = document.getElementById('inspectionFormBtn');
        if (inspectionFormBtn) {
            inspectionFormBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.openNotesFormPopup();
            });
        }
        
        // Navigation buttons
        const prevBtn = document.getElementById('prevObjectBtn');
        const nextBtn = document.getElementById('nextObjectBtn');
        
        if (prevBtn) {
            prevBtn.addEventListener('click', () => this.navigateToPrevious());
        }
        
        if (nextBtn) {
            nextBtn.addEventListener('click', () => this.navigateToNext());
        }
        
        // Keyboard navigation
        document.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowLeft') {
                this.navigateToPrevious();
            } else if (e.key === 'ArrowRight') {
                this.navigateToNext();
            }
        });
    }
    
    async handleDownloadAll() {
        const downloadBtn = document.getElementById('downloadAllBtn');
        if (!downloadBtn) return;
        
        // Update button to show progress
        const originalText = downloadBtn.innerHTML;
        downloadBtn.disabled = true;
        downloadBtn.innerHTML = '⏳ Preparing ZIP...';
        
        try {
            // Create a new JSZip instance
            const zip = new JSZip();
            
            // Collect all files to download
            const filesToDownload = [];
            
            // Add grating files
            if (this.objectData.gratings) {
                for (const grating of this.objectData.gratings) {
                    const gratingFiles = this.objectData.files?.[grating] || {};
                    
                    if (gratingFiles.spec) {
                        filesToDownload.push({
                            path: `data/nirspec/${gratingFiles.spec}`,
                            zipPath: `${grating}/${gratingFiles.spec.split('/').pop()}`
                        });
                    }
                    
                    if (gratingFiles.spec_plot) {
                        filesToDownload.push({
                            path: `data/nirspec/${gratingFiles.spec_plot}`,
                            zipPath: `${grating}/${gratingFiles.spec_plot.split('/').pop()}`
                        });
                    }
                }
            }
            
            // Add redshift files
            const files = this.objectData.files || {};
            if (files.zfit) {
                filesToDownload.push({
                    path: `data/nirspec/${files.zfit}`,
                    zipPath: `redshift/${files.zfit.split('/').pop()}`
                });
            }
            
            if (files.zfit_plot) {
                filesToDownload.push({
                    path: `data/nirspec/${files.zfit_plot}`,
                    zipPath: `redshift/${files.zfit_plot.split('/').pop()}`
                });
            }
            
            // Download each file and add to ZIP
            let completed = 0;
            const total = filesToDownload.length;
            
            for (const file of filesToDownload) {
                try {
                    downloadBtn.innerHTML = `⏳ Downloading ${completed + 1}/${total}...`;
                    
                    const response = await fetch(file.path);
                    if (!response.ok) {
                        console.error(`Failed to download ${file.path}`);
                        continue;
                    }
                    
                    const blob = await response.blob();
                    zip.file(file.zipPath, blob);
                    completed++;
                } catch (error) {
                    console.error(`Error downloading ${file.path}:`, error);
                }
            }
            
            // Generate the ZIP file
            downloadBtn.innerHTML = '📦 Generating ZIP...';
            const zipBlob = await zip.generateAsync({
                type: 'blob',
                compression: 'DEFLATE',
                compressionOptions: { level: 6 }
            });
            
            // Create download link
            const url = URL.createObjectURL(zipBlob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${this.objectData.id}_nirspec_data.zip`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            
            // Success message
            downloadBtn.innerHTML = '✅ Downloaded!';
            setTimeout(() => {
                downloadBtn.innerHTML = originalText;
                downloadBtn.disabled = false;
            }, 2000);
            
        } catch (error) {
            console.error('Error creating ZIP:', error);
            downloadBtn.innerHTML = '❌ Download failed';
            setTimeout(() => {
                downloadBtn.innerHTML = originalText;
                downloadBtn.disabled = false;
            }, 3000);
        }
    }
    
    
    navigateToPrevious() {
        try {
            if (!this.hasValidContext || this.currentIndex <= 0) {
                console.log('Cannot navigate to previous: at beginning or no valid context');
                return;
            }
            
            const previousObject = this.navigationObjects[this.currentIndex - 1];
            if (previousObject && previousObject.id) {
                const url = this.generateNavigationUrl(previousObject.id, this.navigationContext);
                window.location.href = url;
            } else {
                console.error('Previous object not found or invalid');
            }
        } catch (error) {
            console.error('Error navigating to previous object:', error);
        }
    }
    
    navigateToNext() {
        try {
            if (!this.hasValidContext || this.currentIndex >= this.totalCount - 1) {
                console.log('Cannot navigate to next: at end or no valid context');
                return;
            }
            
            const nextObject = this.navigationObjects[this.currentIndex + 1];
            if (nextObject && nextObject.id) {
                const url = this.generateNavigationUrl(nextObject.id, this.navigationContext);
                window.location.href = url;
            } else {
                console.error('Next object not found or invalid');
            }
        } catch (error) {
            console.error('Error navigating to next object:', error);
        }
    }
    
    /**
     * Generate URL for navigating to another object while preserving context
     */
    generateNavigationUrl(objectId, context) {
        const contextParam = btoa(JSON.stringify(context));
        return `nirspec_object.html?id=${objectId}&context=${contextParam}`;
    }
    
    // Helper method to get consensus quality label
    getConsensusQualityLabel(quality) {
        // Handle new 0-4 quality scale
        if (window.flagManager?.initialized) {
            try {
                const qualityInfo = window.flagManager.getQualityInfo(quality);
                return `<span style="color: ${qualityInfo.color};" title="${qualityInfo.description}">${qualityInfo.icon} ${qualityInfo.label}</span>`;
            } catch (error) {
                console.warn('Failed to get quality info from FlagManager:', error);
            }
        }
        
        // Fallback for old scale or when FlagManager not available
        const qualityMap = {
            0: { color: '#e0e0e0', label: 'Not Inspected', icon: '⚪' },
            1: { color: '#dc3545', label: 'Bad', icon: '🔴' },
            2: { color: '#ffc107', label: 'Tentative', icon: '🟡' },
            3: { color: '#ff9800', label: 'Probable', icon: '🟠' },
            4: { color: '#28a745', label: 'Secure', icon: '🟢' },
            // Legacy support
            '-1': { color: '#dc3545', label: 'Bad', icon: '🔴' }
        };
        
        const qualityInfo = qualityMap[String(quality)];
        if (qualityInfo) {
            return `<span style="color: ${qualityInfo.color};" title="${qualityInfo.label}">${qualityInfo.icon} ${qualityInfo.label}</span>`;
        }
        
        return '<span style="color: #999;">Unknown</span>';
    }
    
    // Helper method to format flags as enhanced badges with colors and icons
    formatFlags(flags, flagType = 'object_flags') {
        let flagKeys = [];
        
        // Handle different input types
        if (Array.isArray(flags)) {
            // Old string array format
            flagKeys = flags;
        } else if (typeof flags === 'number' && window.flagManager?.initialized) {
            // New bitmask format
            try {
                flagKeys = window.flagManager.decodeBitmask(flagType, flags);
            } catch (error) {
                console.warn(`Failed to decode ${flagType} bitmask:`, error);
                return '<span style="color: #999;">Error decoding flags</span>';
            }
        } else if (!flags || (Array.isArray(flags) && flags.length === 0)) {
            return '<span style="color: #999;">None</span>';
        } else {
            console.warn('Unknown flag format:', flags);
            return '<span style="color: #999;">Unknown format</span>';
        }
        
        if (flagKeys.length === 0) {
            return '<span style="color: #999;">None</span>';
        }
        
        return flagKeys.map(flag => {
            // Try to get definition from FlagManager first, fallback to hardcoded
            let def;
            if (window.flagManager?.initialized) {
                try {
                    def = window.flagManager.getFlagDefinition(flagType, flag);
                } catch (error) {
                    console.warn(`Failed to get definition for ${flagType} flag '${flag}':`, error);
                    // Fallback to hardcoded definitions
                    def = FLAG_DEFINITIONS[flagType]?.[flag];
                }
            } else {
                def = FLAG_DEFINITIONS[flagType]?.[flag];
            }
            
            if (def) {
                return `<span class="flag-badge" 
                            style="display: inline-block; padding: 4px 8px; margin: 0 3px 2px 0; 
                                   background: ${def.color}; color: #333; border-radius: 4px; 
                                   font-size: 0.85em; font-weight: 500; cursor: help;" 
                            title="${def.description}">
                    ${def.icon} ${def.label}
                </span>`;
            } else {
                // Fallback for unknown flags
                return `<span class="flag-badge" 
                            style="display: inline-block; padding: 4px 8px; margin: 0 3px 2px 0; 
                                   background: #e0e0e0; color: #333; border-radius: 4px; 
                                   font-size: 0.85em; font-weight: 500;">
                    ${flag}
                </span>`;
            }
        }).join('');
    }
    
    // Helper method to format publications as badges
    formatPublications(publications) {
        if (!publications || publications.length === 0) {
            return '<span style="color: #999;">None</span>';
        }
        return publications.map(pub => {
            const name = pub.name || pub.id || 'Publication';
            if (pub.url) {
                return `<a href="${pub.url}" 
                           target="_blank" 
                           class="publication-badge"
                           title="View publication: ${name}">
                    📄 ${name}
                </a>`;
            }
            return `<span class="publication-badge" title="Publication: ${name}">
                📄 ${name}
            </span>`;
        }).join('');
    }
    
    // Helper method to format notes history
    formatNotesHistory(notesHistory) {
        let content = '';
        
        if (!notesHistory || notesHistory.length === 0) {
            content = `
                <div style="text-align: center; padding: 2rem; color: #666;">
                    <p>No notes have been submitted for this object yet.</p>
                </div>
            `;
        } else {
            // Sort by timestamp, newest first
            const sortedNotes = [...notesHistory].sort((a, b) => {
                const dateA = new Date(a.timestamp);
                const dateB = new Date(b.timestamp);
                return dateB - dateA;
            });
            
            content = `
                <div class="notes-timeline" style="max-height: 400px; overflow-y: auto; padding: 1rem;">
                    ${sortedNotes.map(note => this.formatSingleNote(note)).join('')}
                </div>
            `;
        }
        
        // Add call-to-action at the bottom for both cases
        content += `
            <div style="border-top: 1px solid #e0e0e0; margin-top: 1.5rem; padding-top: 1.5rem; text-align: center;">
                <p style="margin-bottom: 1rem; color: #666;">
                    To add your own notes, redshift assessments, or quality flags:
                </p>
                <button 
                    class="btn-hero primary" 
                    onclick="nirspecObject.openNotesFormPopup()"
                    style="display: inline-block; padding: 0.5rem 1.5rem; cursor: pointer;">
                    📝 Open Inspection Form
                </button>
            </div>
        `;
        
        return content;
    }
    
    // Helper method to format a single note entry
    formatSingleNote(note) {
        const timestamp = note.timestamp ? new Date(note.timestamp).toLocaleString() : 'Unknown date';
        const author = note.author || 'Anonymous';
        
        return `
            <div style="border-left: 3px solid #007bff; padding-left: 1rem; margin-bottom: 1.5rem;">
                <div style="margin-bottom: 0.5rem;">
                    <strong style="color: #333;">${author}</strong>
                    <span style="color: #999; font-size: 0.85em; margin-left: 0.5rem;">${timestamp}</span>
                </div>
                <div style="color: #555; margin-bottom: 0.5rem;">
                    ${note.note_text || '<em>No text provided</em>'}
                </div>
                ${note.redshift_override ? `
                    <div style="font-size: 0.9em; color: #666;">
                        <strong>Redshift Override:</strong> ${note.redshift_override}
                        ${note.quality_flag ? ` (Quality: ${note.quality_flag})` : ''}
                        ${note.confidence ? ` (Confidence: ${note.confidence}%)` : ''}
                    </div>
                ` : ''}
            </div>
        `;
    }
    
    // Method to open Tally form in popup window
    openNotesFormPopup() {
        const tallyUrl = `https://tally.so/r/w7ARzR?id=${encodeURIComponent(this.objectId)}`;
        
        // Calculate center position
        const width = 600;
        const height = 800;
        const left = Math.max(0, (screen.width - width) / 2);
        const top = Math.max(0, (screen.height - height) / 2);
        
        // Open popup window
        const popup = window.open(
            tallyUrl,
            'tallyNotesForm',
            `width=${width},height=${height},left=${left},top=${top},toolbar=no,menubar=no,scrollbars=yes,resizable=no,location=no,status=no`
        );
        
        // Check if popup was blocked
        if (!popup || popup.closed || typeof popup.closed === 'undefined') {
            // Fallback to opening in new tab
            alert('Popup was blocked. Opening form in a new tab instead.\n\nPlease allow popups for this site to use the popup window feature.');
            window.open(tallyUrl, '_blank');
        } else {
            // Focus the popup
            popup.focus();
        }
    }
    
    showError(message) {
        const container = document.querySelector('.tab-content-container');
        if (container) {
            container.innerHTML = `
                <div style="text-align: center; padding: 3rem; color: #dc3545;">
                    <h3>Error</h3>
                    <p>${message}</p>
                    <a href="nirspec.html" class="btn-hero">← Back to List</a>
                </div>
            `;
        }
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.nirspecObject = new NIRSpecObject();
});
