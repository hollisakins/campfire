// JWST Data CDN - Basic JavaScript functionality

// Set active navigation item based on current page
document.addEventListener('DOMContentLoaded', () => {
    const navLinks = document.querySelectorAll('.navbar-nav a');
    const currentPath = window.location.pathname;
    console.log('currentPath:', currentPath);
    
    navLinks.forEach(link => {
        // Reset all links
        link.classList.remove('active');
        
        // Set active based on current page
        if (currentPath.includes('nircam') && link.textContent === 'NIRCam') {
            link.classList.add('active');
        } else if (currentPath.includes('nirspec') && link.href.includes('nirspec')) {
            link.classList.add('active');
        } else if (currentPath.endsWith('/') || currentPath.includes('index.html')) {
            if (link.href.includes('index.html')) {
                link.classList.add('active');
            }
        }
    });
    
    // Load markdown reduction notes and data manifest if on a field page
    if (currentPath.includes('nircam-cosmos')) {
        loadReductionNotes('cosmos');
        loadDataManifest('cosmos');
        initializeExposureMapViewer('cosmos');
        initializeFiltering();
        initializePagination();
        initializeDownloadControls();
    } else if (currentPath.includes('nircam-uds')) {
        loadReductionNotes('uds');
        loadDataManifest('uds');
        initializeExposureMapViewer('uds');
        initializeFiltering();
        initializePagination();
        initializeDownloadControls();
    } else if (currentPath === '/' || currentPath.endsWith('/') || currentPath.includes('index.html') || currentPath === '') {
        // Load usage policy on index page
        console.log('Loading usage policy for index page');
        loadUsagePolicy();
    }
});

// Configure marked with custom renderer for header shifting
function configureMarkdownRenderer() {
    const renderer = new marked.Renderer();
    
    // Shift all headers down one level
    renderer.heading = function(text, level) {
        const shiftedLevel = Math.min(level + 1, 6); // Cap at h6
        return `<h${shiftedLevel}>${text}</h${shiftedLevel}>`;
    };
    
    marked.setOptions({
        breaks: true,
        gfm: true,
        renderer: renderer,
        highlight: function(code) {
            return code; // Simple highlighting, can be enhanced later
        }
    });
}

// Load and render markdown reduction notes
async function loadReductionNotes(field) {
    const notesContainer = document.getElementById('reduction-notes');
    if (!notesContainer) return;
    
    try {
        const response = await fetch(`build/docs/nircam-${field}-reduction-notes.md`);
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const markdown = await response.text();
        
        // Configure marked with header shifting
        configureMarkdownRenderer();
        
        // Parse and render the markdown
        const html = marked.parse(markdown);
        notesContainer.innerHTML = html;
        
        // Hide the card title since markdown provides its own title
        const cardTitle = notesContainer.closest('.card')?.querySelector('.card-title');
        if (cardTitle) {
            cardTitle.style.display = 'none';
        }
        
    } catch (error) {
        // Display error message if file not found or other error
        notesContainer.innerHTML = `
            <div class="text-muted">
                <p>No reduction notes available yet.</p>
                <p class="small">Expected location: <code>build/docs/nircam-${field}-reduction-notes.md</code></p>
            </div>
        `;
        console.error('Error loading reduction notes:', error);
    }
}

// Load and render usage policy markdown
async function loadUsagePolicy() {
    const usagePolicyContainer = document.getElementById('usage-policy');
    if (!usagePolicyContainer) return;
    
    try {
        const response = await fetch('build/docs/USAGE_POLICY.md');
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const markdown = await response.text();
        
        // Configure marked with header shifting
        configureMarkdownRenderer();
        
        // Parse and render the markdown
        const html = marked.parse(markdown);
        usagePolicyContainer.innerHTML = html;
        
        // Hide the card title since markdown provides its own title
        const cardTitle = usagePolicyContainer.closest('.card')?.querySelector('.card-title');
        if (cardTitle) {
            cardTitle.style.display = 'none';
        }
        
    } catch (error) {
        usagePolicyContainer.innerHTML = `
            <div class="text-muted">
                <p>No usage policy available yet.</p>
                <p class="small">Expected location: <code>build/docs/USAGE_POLICY.md</code></p>
            </div>
        `;
        console.error('Error loading usage policy:', error);
    }
}

// Load and render TOML data manifest
async function loadDataManifest(field) {
    const tableBody = document.getElementById('data-table-body');
    if (!tableBody) return;
    
    try {
        const response = await fetch(`static/manifests/nircam-${field}-manifest.toml`);
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        
        const tomlText = await response.text();
        
        // Parse TOML using the @iarna/toml library
        const manifest = TOML.parse(tomlText);
        
        // Render the data table
        renderDataTable(manifest, tableBody);
        
    } catch (error) {
        // Display error message if file not found or other error
        tableBody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-muted">
                    <p>No data manifest available yet.</p>
                    <p class="small">Expected location: <code>static/manifests/nircam-${field}-manifest.toml</code></p>
                </td>
            </tr>
        `;
        console.error('Error loading data manifest:', error);
    }
}

// Render data table from manifest
function renderDataTable(manifest, tableBody) {
    // Store manifest globally
    currentManifest = manifest;
    
    // Clear loading spinner
    tableBody.innerHTML = '';
    
    // Check if files exist
    if (!manifest.files || manifest.files.length === 0) {
        tableBody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-muted">
                    No data files available yet.
                </td>
            </tr>
        `;
        return;
    }
    
    // Populate filter dropdowns
    populateFilterDropdowns(manifest);
    
    // Render each file as a table row
    manifest.files.forEach(file => {
        const row = document.createElement('tr');
        
        // Add data attributes for filtering
        const sizeBytes = parseFileSize(file.size);
        row.dataset.filename = file.filename;
        row.dataset.filter = file.filter || '';
        row.dataset.tile = file.tile || '';
        row.dataset.pixelScale = file.pixel_scale || '';
        row.dataset.version = file.version || '';
        row.dataset.extension = file.extension || '';
        row.dataset.size = file.size || '';
        row.dataset.sizeBytes = sizeBytes;
        row.dataset.path = file.path || file.filename;
        row.dataset.description = file.description || '';
        
        row.innerHTML = `
            <td><code class="code-cell">${file.filter || '-'}</code></td>
            <td><code class="code-cell">${file.tile || '-'}</code></td>
            <td><code class="code-cell">${file.pixel_scale || '-'}</code></td>
            <td><code class="code-cell">${file.version || '-'}</code></td>
            <td><code class="code-cell">${file.extension || '-'}</code></td>
            <td>
                <a href="${file.path || file.filename}" 
                   style="font-style: italic; text-decoration: underline;" 
                   download="${file.filename}">
                    Download${file.size ? ' (' + file.size + ')' : ''}
                </a>
            </td>
        `;
        
        // Add title attribute for description if available
        if (file.description) {
            row.querySelector('td:nth-child(1)').title = file.description;
        }
        
        tableBody.appendChild(row);
    });
    
    // Initialize filter summary
    updateFilterSummary(manifest.files.length, manifest.files.reduce((sum, file) => sum + parseFileSize(file.size), 0));
    
    // Add metadata info if available
    if (manifest.metadata) {
        const metadataDiv = document.getElementById('field-metadata');
        if (metadataDiv && manifest.metadata.total_size) {
            metadataDiv.innerHTML = `
                <p class="text-muted">
                    Total size: ${manifest.metadata.total_size} | 
                    Last updated: ${manifest.metadata.last_updated || 'Unknown'}
                </p>
            `;
        }
    }
    
    // Populate exposure map filter tabs
    populateFilterTabs();
    
    // Initialize selected counts
    updateSelectedCounts();
    
    // Apply initial pagination
    applyFilters();
}

// Global variables for filtering and pagination
let currentManifest = null;
let filterState = {
    filter: [],
    tile: [],
    pixelScale: [],
    version: [],
    extension: []
};

// Pagination state
let paginationState = {
    currentPage: 1,
    itemsPerPage: 10,
    totalPages: 1,
    totalItems: 0
};

// Initialize filtering controls
function initializeFiltering() {
    const clearFilters = document.getElementById('clear-filters');
    
    // Add event listeners
    if (clearFilters) {
        clearFilters.addEventListener('click', clearAllFilters);
    }
    
    // Initialize filter toggle functionality
    initializeFilterToggles();
    
    // Initialize table sorting
    initializeTableSorting();
}

// Initialize filter toggle functionality
function initializeFilterToggles() {
    const filterToggles = document.querySelectorAll('.filter-toggle');
    
    filterToggles.forEach(toggle => {
        toggle.addEventListener('click', (e) => {
            e.preventDefault();
            const targetId = toggle.getAttribute('data-target');
            const targetContainer = document.getElementById(targetId);
            
            if (targetContainer) {
                toggleFilterSection(toggle, targetContainer);
            }
        });
    });
}

// Toggle filter section visibility
function toggleFilterSection(toggle, container) {
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

// Update selected count display for filter toggles
function updateSelectedCounts() {
    const filterTypes = ['filter', 'tile', 'pixel-scale', 'version', 'extension'];
    const filterStateKeys = ['filter', 'tile', 'pixelScale', 'version', 'extension'];
    
    filterTypes.forEach((type, index) => {
        const countElement = document.getElementById(`${type}-count`);
        const filterKey = filterStateKeys[index];
        
        if (countElement) {
            const selectedCount = filterState[filterKey].length;
            if (selectedCount > 0) {
                countElement.textContent = selectedCount;
            } else {
                countElement.textContent = '';
            }
        }
    });
}

// Initialize download controls
function initializeDownloadControls() {
    const generateScript = document.getElementById('generate-curl-script');
    const downloadScript = document.getElementById('download-script');
    const copyScript = document.getElementById('copy-script');
    const hideScript = document.getElementById('hide-script');
    
    if (generateScript) {
        generateScript.addEventListener('click', (e) => {
            e.preventDefault();
            toggleScriptPreview();
        });
    }
    
    if (downloadScript) {
        downloadScript.addEventListener('click', downloadScriptFile);
    }
    
    if (copyScript) {
        copyScript.addEventListener('click', copyScriptToClipboard);
    }
    
    if (hideScript) {
        hideScript.addEventListener('click', () => {
            document.getElementById('script-preview').style.display = 'none';
        });
    }
}

// Populate filter checkboxes from manifest data
function populateFilterDropdowns(manifest) {
    if (!manifest.files) return;
    
    const filterContainer = document.getElementById('filter-checkboxes');
    const tileContainer = document.getElementById('tile-checkboxes');
    const pixelScaleContainer = document.getElementById('pixel-scale-checkboxes');
    const versionContainer = document.getElementById('version-checkboxes');
    const extensionContainer = document.getElementById('extension-checkboxes');
    
    // Get unique values for each field
    const filters = [...new Set(manifest.files.map(f => f.filter).filter(f => f))];
    const tiles = [...new Set(manifest.files.map(f => f.tile).filter(t => t))];
    const pixelScales = [...new Set(manifest.files.map(f => f.pixel_scale).filter(p => p))];
    const versions = [...new Set(manifest.files.map(f => f.version).filter(v => v))];
    const extensions = [...new Set(manifest.files.map(f => f.extension).filter(e => e))];
    
    // Populate filter checkboxes
    if (filterContainer) {
        filterContainer.innerHTML = '';
        filters.forEach(filter => {
            const checkboxItem = createCheckboxItem(filter, filter, 'filter');
            filterContainer.appendChild(checkboxItem);
        });
    }
    
    // Populate tile checkboxes (sorted alphanumerically: A1, A2, A10, B1, etc.)
    if (tileContainer) {
        tileContainer.innerHTML = '';
        const sortedTiles = tiles.sort((a, b) => {
            // Extract letter and number parts
            const aMatch = a.match(/^([A-Z]+)(\d+)$/);
            const bMatch = b.match(/^([A-Z]+)(\d+)$/);
            
            if (aMatch && bMatch) {
                const [, aLetter, aNumber] = aMatch;
                const [, bLetter, bNumber] = bMatch;
                
                // First compare letters
                if (aLetter !== bLetter) {
                    return aLetter.localeCompare(bLetter);
                }
                
                // Then compare numbers numerically
                return parseInt(aNumber, 10) - parseInt(bNumber, 10);
            }
            
            // Fallback to string comparison for non-standard formats
            return a.localeCompare(b);
        });
        
        sortedTiles.forEach(tile => {
            const checkboxItem = createCheckboxItem(tile, tile, 'tile');
            tileContainer.appendChild(checkboxItem);
        });
    }
    
    // Populate pixel scale checkboxes
    if (pixelScaleContainer) {
        pixelScaleContainer.innerHTML = '';
        pixelScales.forEach(scale => {
            const checkboxItem = createCheckboxItem(scale, scale, 'pixelScale');
            pixelScaleContainer.appendChild(checkboxItem);
        });
    }
    
    // Populate version checkboxes
    if (versionContainer) {
        versionContainer.innerHTML = '';
        versions.forEach(version => {
            const checkboxItem = createCheckboxItem(version, version, 'version');
            versionContainer.appendChild(checkboxItem);
        });
    }
    
    // Populate extension checkboxes (sorted by priority: SCI > ERR > WHT)
    if (extensionContainer) {
        extensionContainer.innerHTML = '';
        const extensionOrder = ['SCI', 'ERR', 'WHT'];
        const sortedExtensions = extensions.sort((a, b) => {
            const indexA = extensionOrder.indexOf(a);
            const indexB = extensionOrder.indexOf(b);
            // If extension not in our order list, put it at the end
            if (indexA === -1) return 1;
            if (indexB === -1) return -1;
            return indexA - indexB;
        });
        
        sortedExtensions.forEach(extension => {
            const checkboxItem = createCheckboxItem(extension, extension, 'extension');
            extensionContainer.appendChild(checkboxItem);
        });
    }
}

// Create a checkbox item for filter groups
function createCheckboxItem(value, displayText, filterType) {
    const item = document.createElement('div');
    item.className = 'checkbox-item';
    
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.id = `${filterType}-${value}`;
    checkbox.value = value;
    
    const label = document.createElement('label');
    label.htmlFor = checkbox.id;
    label.textContent = displayText;
    
    checkbox.addEventListener('change', () => {
        if (checkbox.checked) {
            if (!filterState[filterType].includes(value)) {
                filterState[filterType].push(value);
            }
        } else {
            const index = filterState[filterType].indexOf(value);
            if (index > -1) {
                filterState[filterType].splice(index, 1);
            }
        }
        applyFilters();
        updateCurlScriptText();
        updateSelectedCounts();
    });
    
    item.appendChild(checkbox);
    item.appendChild(label);
    
    return item;
}

// Collapse curl script dialog when filters change
function collapseScriptDialog() {
    const scriptPreview = document.getElementById('script-preview');
    const generateScript = document.getElementById('generate-curl-script');
    const toggleIcon = generateScript?.querySelector('.toggle-icon');
    
    if (scriptPreview && scriptPreview.style.display !== 'none') {
        scriptPreview.style.display = 'none';
        // Reset arrow to pointing right
        if (toggleIcon) {
            toggleIcon.textContent = '▶';
        }
    }
}

// Apply filters and pagination to table
function applyFilters() {
    // Hide curl script dialog when filters change to avoid confusion
    collapseScriptDialog();
    
    const rows = document.querySelectorAll('#data-table-body tr');
    let filteredRows = [];
    let totalSize = 0;
    
    // First pass: apply filters and collect matching rows
    rows.forEach(row => {
        if (!row.dataset.filename) return; // Skip loading/error rows
        
        const matchesFilter = matchesFilterFilter(row);
        const matchesTile = matchesTileFilter(row);
        const matchesPixelScale = matchesPixelScaleFilter(row);
        const matchesVersion = matchesVersionFilter(row);
        const matchesExtension = matchesExtensionFilter(row);
        
        if (matchesFilter && matchesTile && matchesPixelScale && matchesVersion && matchesExtension) {
            filteredRows.push(row);
            if (row.dataset.sizeBytes) {
                totalSize += parseFloat(row.dataset.sizeBytes);
            }
        }
    });
    
    // Update pagination state
    paginationState.totalItems = filteredRows.length;
    paginationState.totalPages = Math.ceil(filteredRows.length / paginationState.itemsPerPage);
    
    // Reset to page 1 if current page is now invalid
    if (paginationState.currentPage > paginationState.totalPages) {
        paginationState.currentPage = 1;
    }
    
    // Second pass: apply pagination
    const startIndex = (paginationState.currentPage - 1) * paginationState.itemsPerPage;
    const endIndex = startIndex + paginationState.itemsPerPage;
    
    rows.forEach(row => {
        row.style.display = 'none'; // Hide all rows first
    });
    
    filteredRows.forEach((row, index) => {
        if (index >= startIndex && index < endIndex) {
            row.style.display = ''; // Show rows for current page
        }
    });
    
    updateFilterSummary(filteredRows.length, totalSize);
    updatePaginationControls();
}

// Filter matching functions
function matchesFilterFilter(row) {
    if (!filterState.filter.length) return true;
    return filterState.filter.includes(row.dataset.filter);
}

function matchesTileFilter(row) {
    if (!filterState.tile.length) return true;
    return filterState.tile.includes(row.dataset.tile);
}

function matchesPixelScaleFilter(row) {
    if (!filterState.pixelScale.length) return true;
    return filterState.pixelScale.includes(row.dataset.pixelScale);
}

function matchesVersionFilter(row) {
    if (!filterState.version.length) return true;
    return filterState.version.includes(row.dataset.version);
}

function matchesExtensionFilter(row) {
    if (!filterState.extension.length) return true;
    return filterState.extension.includes(row.dataset.extension);
}

// Update filter summary
function updateFilterSummary(visibleCount, visibleSizeBytes) {
    const visibleCountEl = document.getElementById('visible-count');
    const totalCountEl = document.getElementById('total-count');
    const visibleSizeEl = document.getElementById('visible-size');
    
    if (visibleCountEl) visibleCountEl.textContent = visibleCount;
    if (totalCountEl) {
        const totalRows = document.querySelectorAll('#data-table-body tr[data-filename]').length;
        totalCountEl.textContent = totalRows;
    }
    if (visibleSizeEl) {
        visibleSizeEl.textContent = formatFileSize(visibleSizeBytes);
    }
}

// Initialize pagination controls
function initializePagination() {
    const itemsPerPageSelect = document.getElementById('items-per-page');
    const prevPageBtn = document.getElementById('prev-page');
    const nextPageBtn = document.getElementById('next-page');
    const firstPageBtn = document.getElementById('first-page');
    const lastPageBtn = document.getElementById('last-page');
    
    if (itemsPerPageSelect) {
        itemsPerPageSelect.addEventListener('change', (e) => {
            paginationState.itemsPerPage = parseInt(e.target.value);
            paginationState.currentPage = 1;
            applyFilters();
        });
    }
    
    if (prevPageBtn) {
        prevPageBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (paginationState.currentPage > 1) {
                paginationState.currentPage--;
                applyFilters();
            }
        });
    }
    
    if (nextPageBtn) {
        nextPageBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (paginationState.currentPage < paginationState.totalPages) {
                paginationState.currentPage++;
                applyFilters();
            }
        });
    }
    
    if (firstPageBtn) {
        firstPageBtn.addEventListener('click', (e) => {
            e.preventDefault();
            paginationState.currentPage = 1;
            applyFilters();
        });
    }
    
    if (lastPageBtn) {
        lastPageBtn.addEventListener('click', (e) => {
            e.preventDefault();
            paginationState.currentPage = paginationState.totalPages;
            applyFilters();
        });
    }
}

// Update pagination controls visibility and state
function updatePaginationControls() {
    const paginationContainer = document.getElementById('pagination-controls');
    const prevPageBtn = document.getElementById('prev-page');
    const nextPageBtn = document.getElementById('next-page');
    const firstPageBtn = document.getElementById('first-page');
    const lastPageBtn = document.getElementById('last-page');
    const pageInfo = document.getElementById('page-info');
    
    if (!paginationContainer) return;
    
    // Show/hide pagination based on whether it's needed
    if (paginationState.totalPages <= 1) {
        paginationContainer.style.display = 'none';
        return;
    } else {
        paginationContainer.style.display = 'flex';
    }
    
    // Update button states
    if (prevPageBtn) {
        prevPageBtn.disabled = paginationState.currentPage <= 1;
        prevPageBtn.classList.toggle('disabled', paginationState.currentPage <= 1);
    }
    
    if (nextPageBtn) {
        nextPageBtn.disabled = paginationState.currentPage >= paginationState.totalPages;
        nextPageBtn.classList.toggle('disabled', paginationState.currentPage >= paginationState.totalPages);
    }
    
    if (firstPageBtn) {
        firstPageBtn.disabled = paginationState.currentPage <= 1;
        firstPageBtn.classList.toggle('disabled', paginationState.currentPage <= 1);
    }
    
    if (lastPageBtn) {
        lastPageBtn.disabled = paginationState.currentPage >= paginationState.totalPages;
        lastPageBtn.classList.toggle('disabled', paginationState.currentPage >= paginationState.totalPages);
    }
    
    // Update page info
    if (pageInfo) {
        const start = (paginationState.currentPage - 1) * paginationState.itemsPerPage + 1;
        const end = Math.min(start + paginationState.itemsPerPage - 1, paginationState.totalItems);
        pageInfo.textContent = `${start}-${end} of ${paginationState.totalItems}`;
    }
}

// Clear all filters
function clearAllFilters() {
    filterState = { filter: [], tile: [], pixelScale: [], version: [], extension: [] };
    
    // Clear all checkboxes
    const checkboxes = document.querySelectorAll('.checkbox-item input[type="checkbox"]');
    checkboxes.forEach(checkbox => {
        checkbox.checked = false;
    });
    
    applyFilters();
    updateCurlScriptText();
    updateSelectedCounts();
}

// Update curl script link text based on filter state
function updateCurlScriptText() {
    const curlScriptLink = document.getElementById('generate-curl-script');
    if (!curlScriptLink) return;
    
    const hasFilters = filterState.filter.length > 0 || 
                      filterState.tile.length > 0 || 
                      filterState.pixelScale.length > 0 || 
                      filterState.version.length > 0 || 
                      filterState.extension.length > 0;
    
    const textSpan = curlScriptLink.querySelector('span:last-child');
    if (textSpan) {
        textSpan.textContent = hasFilters ? 
            'Curl script to download filtered files' : 
            'Curl script to download files';
    }
}

// Toggle script preview visibility with arrow rotation
function toggleScriptPreview() {
    const scriptPreview = document.getElementById('script-preview');
    const generateScript = document.getElementById('generate-curl-script');
    const toggleIcon = generateScript?.querySelector('.toggle-icon');
    
    if (!scriptPreview) return;
    
    const isVisible = scriptPreview.style.display !== 'none';
    
    if (isVisible) {
        // Hide the script preview
        scriptPreview.style.display = 'none';
        if (toggleIcon) {
            toggleIcon.textContent = '▶';
        }
    } else {
        // Generate and show the script preview
        generateDownloadScript();
        if (toggleIcon) {
            toggleIcon.textContent = '▼';
        }
    }
}

// Get visible files for download script generation (all filtered files, not just current page)
function getVisibleFiles() {
    const rows = document.querySelectorAll('#data-table-body tr');
    let filteredFiles = [];
    
    rows.forEach(row => {
        if (!row.dataset.filename) return; // Skip loading/error rows
        
        const matchesFilter = matchesFilterFilter(row);
        const matchesTile = matchesTileFilter(row);
        const matchesPixelScale = matchesPixelScaleFilter(row);
        const matchesVersion = matchesVersionFilter(row);
        const matchesExtension = matchesExtensionFilter(row);
        
        if (matchesFilter && matchesTile && matchesPixelScale && matchesVersion && matchesExtension) {
            filteredFiles.push({
                filename: row.dataset.filename,
                path: row.dataset.path,
                size: row.dataset.size,
                filter: row.dataset.filter,
                tile: row.dataset.tile,
                pixelScale: row.dataset.pixelScale,
                version: row.dataset.version,
                extension: row.dataset.extension,
                sizeBytes: parseFloat(row.dataset.sizeBytes) || 0
            });
        }
    });
    
    return filteredFiles;
}

// Generate download script
function generateDownloadScript() {
    const visibleFiles = getVisibleFiles();
    if (visibleFiles.length === 0) {
        alert('No files visible. Please adjust your filters.');
        return;
    }
    
    const field = currentManifest?.metadata?.field || 'data';
    const totalSize = visibleFiles.reduce((sum, file) => sum + file.sizeBytes, 0);
    
    let script = `#!/bin/bash
# Generated: ${new Date().toISOString()}
# Field: ${field}
echo "============================="
echo "CAMPFIRE NIRCam Data Download"
echo "============================="
echo ""
echo "This script will download ${visibleFiles.length} files (${formatFileSize(totalSize)} total)"
echo "You need to authenticate with your CAMPFIRE credentials."
echo ""
read -p "Username: " USERNAME
read -s -p "Password: " PASSWORD
echo ""

# Validate credentials are provided
if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
    echo "Error: Username and password are required"
    exit 1
fi

# Create directory structure
mkdir -p ${field.toLowerCase()}_data
cd ${field.toLowerCase()}_data

# Download files with progress bar
# Using curl with:
#   -L: Follow redirects
#   -u: Basic authentication (username:password)
#   --progress-bar: Show progress

BASE_URL="https://data.hollisakins.com"

echo "Starting download of ${visibleFiles.length} files..."
echo "Total size: ${formatFileSize(totalSize)}"
echo ""

`;

    // Add download commands
    visibleFiles.forEach((file, index) => {
        const relativePath = file.path || file.filename;
        const cleanPath = relativePath.replace(/^\//, '');
        const filename = file.filename; // Use just the filename for output
        
        script += `# File ${index + 1}/${visibleFiles.length}: ${filename} (${file.size || 'unknown size'})\n`;
        script += `echo "Downloading ${filename}..."\n`;
        script += `read size time speed <<< $(curl -L -u "$USERNAME:$PASSWORD" --progress-bar -o "${filename}" --write-out "%{size_download} %{time_total} %{speed_download}" "$BASE_URL/${cleanPath}")\n`;
        script += `echo "✓ Downloaded: \$((\${size}/1024/1024)) MB in \${time}s at \$((\${speed}/1024/1024)) MB/s"\n`;
        if (index < visibleFiles.length - 1) {
            script += `echo ""\n\n`;
        }
    });

    script += `\necho ""\necho "Download complete!"\n`;
    script += `echo "Files saved in: $(pwd)"\n`;
    
    // Show script preview
    document.getElementById('script-content').textContent = script;
    document.getElementById('script-preview').style.display = 'block';
}


// Download script as file
function downloadScriptFile() {
    const scriptContent = document.getElementById('script-content').textContent;
    const field = currentManifest?.metadata?.field || 'data';
    const filename = `download_${field.toLowerCase()}_data.sh`;
    
    const blob = new Blob([scriptContent], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

// Copy script to clipboard
async function copyScriptToClipboard() {
    const scriptContent = document.getElementById('script-content').textContent;
    
    try {
        await navigator.clipboard.writeText(scriptContent);
        alert('Download script copied to clipboard!');
    } catch (err) {
        console.error('Failed to copy script:', err);
        alert('Failed to copy script to clipboard.');
    }
}

// Utility functions
function debounce(func, wait) {
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

function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function parseFileSize(sizeStr) {
    if (!sizeStr) return 0;
    const size = parseFloat(sizeStr);
    const unit = sizeStr.toLowerCase();
    
    if (unit.includes('gb')) return size * 1024 * 1024 * 1024;
    if (unit.includes('mb')) return size * 1024 * 1024;
    if (unit.includes('kb')) return size * 1024;
    return size;
}

// Simple utility function for future use
function downloadFile(filename) {
    // Create download link
    const link = document.createElement('a');
    link.href = filename;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

// Exposure Map Viewer functionality
let currentExposureMapField = null;
let availableFilters = [];
let imageCache = {};

// Hardcoded exposure map filters for each field
const exposureMapFilters = {
    'cosmos': ['F090W','F115W','F140M','F150W','F182M','F200W','F210M','F250M','F277W','F335M','F356W','F360M','F410M','F430M','F444W','F460M','F480M'],  
    'uds': ['F070W','F090W','F115W','F140M','F150W','F162M','F182M','F200W','F210M','F250M','F277W','F300M','F335M','F356W','F360M','F410M','F430M','F444W','F460M','F480M']
};

// Initialize exposure map viewer
function initializeExposureMapViewer(field) {
    currentExposureMapField = field;
    // Filter tabs will be populated when the manifest is loaded in renderDataTable
}

// Populate filter tabs using hardcoded configuration
async function populateFilterTabs() {
    const filterTabsContainer = document.getElementById('filter-tabs');
    if (!filterTabsContainer || !currentExposureMapField) return;
    
    // Use hardcoded filters for the current field
    availableFilters = exposureMapFilters[currentExposureMapField] || [];
    
    if (availableFilters.length === 0) {
        // Fallback to manifest data if no hardcoded filters
        if (currentManifest && currentManifest.files) {
            availableFilters = [...new Set(currentManifest.files.map(f => f.filter).filter(f => f))];
            availableFilters.sort();
        }
    }
    
    // Clear loading spinner
    filterTabsContainer.innerHTML = '';
    
    if (availableFilters.length === 0) {
        filterTabsContainer.innerHTML = '<p class="text-muted small">No exposure maps available</p>';
        return;
    }
    
    // Create filter tabs
    availableFilters.forEach((filter, index) => {
        const tab = document.createElement('div');
        tab.className = 'filter-tab';
        tab.textContent = filter;
        tab.dataset.filter = filter;
        
        tab.addEventListener('click', () => {
            selectFilterTab(filter);
        });
        
        filterTabsContainer.appendChild(tab);
        
        // Select first filter by default
        if (index === 0) {
            selectFilterTab(filter);
        }
    });
    
    // Start preloading all images in the background
    preloadExposureMaps();
    
    // Setup keyboard navigation for filter tabs
    setupKeyboardNavigation();
}

// Detect available exposure maps by trying common filter names
// NOTE: This function is kept for reference but not used anymore
// We use hardcoded filter lists in exposureMapFilters to avoid filesystem bottlenecks
/*
async function detectAvailableExposureMaps() {
    const commonFilters = [
        // Short Wavelength Channel (0.6-2.3 μm)
        'F070W', 'F090W', 'F115W', 'F140M', 'F150W', 'F150W2', 'F162M', 'F164N', 
        'F182M', 'F187N', 'F200W', 'F210M', 'F212N',
        // Long Wavelength Channel (2.4-5.0 μm)
        'F250M', 'F277W', 'F300M', 'F322W2', 'F323N', 'F335M', 'F356W', 'F360M', 
        'F405N', 'F410M', 'F430M', 'F444W', 'F460M', 'F466N', 'F470N', 'F480M'
    ];
    const availableFilters = [];
    
    for (const filter of commonFilters) {
        const imagePath = `static/images/nircam/${currentExposureMapField}/exposure_maps/${filter}_exposure.png`;
        
        try {
            const response = await fetch(imagePath, { method: 'HEAD' });
            if (response.ok) {
                availableFilters.push(filter);
            }
        } catch (error) {
            // Image doesn't exist, continue to next filter
            continue;
        }
    }
    
    return availableFilters.sort();
}
*/

// Preload all exposure map images in the background
async function preloadExposureMaps() {
    if (!currentExposureMapField || availableFilters.length === 0) return;
    
    // Preload all available filter images
    const preloadPromises = availableFilters.map(filter => preloadSingleImage(filter));
    
    try {
        await Promise.allSettled(preloadPromises);
        console.log('Exposure map preloading completed');
    } catch (error) {
        console.log('Some exposure maps failed to preload:', error);
    }
}

// Preload a single image and store in cache
function preloadSingleImage(filter) {
    return new Promise((resolve, reject) => {
        const imagePath = `static/images/nircam/${currentExposureMapField}/exposure_maps/${filter}_exposure.png`;
        
        // Skip if already cached
        if (imageCache[filter]) {
            resolve(imageCache[filter]);
            return;
        }
        
        const img = new Image();
        
        img.onload = () => {
            // Store the loaded image element in cache
            img.className = 'exposure-map-image';
            img.alt = `${filter} exposure map for ${currentExposureMapField.toUpperCase()}`;
            imageCache[filter] = img;
            resolve(img);
        };
        
        img.onerror = () => {
            console.log(`Failed to preload exposure map for ${filter}`);
            reject(new Error(`Failed to load ${filter}`));
        };
        
        img.src = imagePath;
    });
}

// Select a filter tab and load its exposure map
function selectFilterTab(filter) {
    // Update tab states
    document.querySelectorAll('.filter-tab').forEach(tab => {
        tab.classList.remove('active');
        if (tab.dataset.filter === filter) {
            tab.classList.add('active');
        }
    });
    
    // Load exposure map image
    loadExposureMap(filter);
}

// Load exposure map image for selected filter
async function loadExposureMap(filter) {
    const imageContainer = document.getElementById('image-container');
    if (!imageContainer) return;
    
    // Check if image is already cached
    if (imageCache[filter]) {
        // Instant display from cache
        imageContainer.innerHTML = '';
        imageContainer.appendChild(imageCache[filter].cloneNode(true));
        return;
    }
    
    // Show loading state for uncached images
    imageContainer.innerHTML = `
        <div class="image-loading">
            <div class="spinner"></div>
        </div>
    `;
    
    try {
        // Try to load and cache the image
        const img = await preloadSingleImage(filter);
        
        // Display the loaded image
        imageContainer.innerHTML = '';
        imageContainer.appendChild(img.cloneNode(true));
        
    } catch (error) {
        console.error('Error loading exposure map:', error);
        showImageError(filter);
    }
}

// Show error message when image fails to load
function showImageError(filter) {
    const imageContainer = document.getElementById('image-container');
    if (!imageContainer) return;
    
    imageContainer.innerHTML = `
        <div class="image-error">
            <h4>Image Not Available</h4>
            <p>Exposure map for ${filter} could not be loaded.</p>
            <p class="small text-muted">Expected location: static/images/nircam/${currentExposureMapField}/exposure_maps/${filter}_exposure.png</p>
        </div>
    `;
}

// Keyboard navigation functions for exposure map viewer
function getCurrentFilterIndex() {
    const activeTab = document.querySelector('.filter-tab.active');
    if (!activeTab || !availableFilters.length) return -1;
    
    const activeFilter = activeTab.dataset.filter;
    return availableFilters.indexOf(activeFilter);
}

function navigateFilterTab(direction) {
    if (!availableFilters.length) return;
    
    const currentIndex = getCurrentFilterIndex();
    if (currentIndex === -1) return;
    
    let nextIndex;
    if (direction === 'up') {
        // Previous filter (with wraparound to last)
        nextIndex = currentIndex === 0 ? availableFilters.length - 1 : currentIndex - 1;
    } else if (direction === 'down') {
        // Next filter (with wraparound to first)
        nextIndex = currentIndex === availableFilters.length - 1 ? 0 : currentIndex + 1;
    } else {
        return;
    }
    
    const nextFilter = availableFilters[nextIndex];
    if (nextFilter) {
        selectFilterTab(nextFilter);
    }
}

function setupKeyboardNavigation() {
    // Remove any existing keyboard listeners to avoid duplicates
    document.removeEventListener('keydown', handleExposureMapKeydown);
    
    // Add new keyboard listener
    document.addEventListener('keydown', handleExposureMapKeydown);
}

function handleExposureMapKeydown(event) {
    // Only handle arrow keys when exposure map viewer is present and has filters
    if (!currentExposureMapField || !availableFilters.length) return;
    
    // Don't interfere if user is typing in an input field
    if (event.target.tagName === 'INPUT' || event.target.tagName === 'TEXTAREA') return;
    
    // Handle up/down arrow keys
    if (event.key === 'ArrowUp') {
        event.preventDefault();
        navigateFilterTab('up');
    } else if (event.key === 'ArrowDown') {
        event.preventDefault();
        navigateFilterTab('down');
    }
}

// Table sorting functionality for NIRCam data tables
let currentSortColumn = null;
let currentSortDirection = 'asc';

function initializeTableSorting() {
    const table = document.querySelector('.data-table');
    if (!table) return;
    
    table.addEventListener('click', (e) => {
        const th = e.target.closest('th.sortable');
        if (!th) return;
        
        const column = th.dataset.column;
        if (!column) return;
        
        // Toggle sort direction if same column, otherwise start with asc
        if (currentSortColumn === column) {
            currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
        } else {
            currentSortColumn = column;
            currentSortDirection = 'asc';
        }
        
        // Update visual indicators
        updateSortIndicators(table, currentSortColumn, currentSortDirection);
        
        // Perform sort
        sortTable(currentSortColumn, currentSortDirection);
    });
}

function updateSortIndicators(table, sortColumn, sortDirection) {
    // Clear all indicators
    table.querySelectorAll('th.sortable').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
    });
    
    // Add indicator to current sort column
    if (sortColumn) {
        const currentTh = table.querySelector(`th[data-column="${sortColumn}"]`);
        if (currentTh) {
            currentTh.classList.add(`sort-${sortDirection}`);
        }
    }
}

function sortTable(column, direction) {
    const tableBody = document.getElementById('data-table-body');
    if (!tableBody) return;
    
    const rows = Array.from(tableBody.querySelectorAll('tr[data-filename]'));
    
    rows.sort((a, b) => {
        let aVal = a.dataset[column] || '';
        let bVal = b.dataset[column] || '';
        
        // Handle null/undefined
        if (aVal == null) aVal = '';
        if (bVal == null) bVal = '';
        
        // Try to parse as numbers for numeric comparison
        const aNum = parseFloat(aVal);
        const bNum = parseFloat(bVal);
        
        let result = 0;
        if (!isNaN(aNum) && !isNaN(bNum)) {
            result = aNum - bNum;
        } else {
            result = String(aVal).localeCompare(String(bVal));
        }
        
        return direction === 'asc' ? result : -result;
    });
    
    // Re-append sorted rows
    rows.forEach(row => tableBody.appendChild(row));
    
    // Re-apply pagination after sorting
    applyFilters();
}