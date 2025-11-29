/**
 * Unified Breadcrumb Navigation Manager for CAMPFIRE
 * Automatically generates breadcrumbs based on site map configuration
 */

class BreadcrumbManager {
    constructor(config = {}) {
        this.containerId = config.containerId || 'breadcrumbNav';
        this.separator = config.separator || '›';
        this.homeTitle = config.homeTitle || 'CAMPFIRE';
        this.showHome = config.showHome !== false; // Default true
        
        // Site map configuration - defines the navigation hierarchy
        this.siteMap = {
            'index.html': {
                title: this.homeTitle,
                parent: null,
                icon: '🏠'
            },
            'nircam-cosmos.html': {
                title: 'COSMOS',
                parent: 'nircam',
                parentTitle: 'NIRCam',
                parentUrl: '#nircam',
                icon: '🏔️'
            },
            'nircam-uds.html': {
                title: 'UDS',
                parent: 'nircam',
                parentTitle: 'NIRCam',
                parentUrl: '#nircam',
                icon: '🏔️'
            },
            'nirspec.html': {
                title: 'NIRSpec',
                parent: 'index.html',
                icon: '🔬'
            },
            'nirspec_object.html': {
                title: 'Object Detail',
                parent: 'nirspec.html',
                dynamic: true,
                icon: '🎯'
            }
        };
        
        // Allow configuration override
        if (config.siteMap) {
            this.siteMap = { ...this.siteMap, ...config.siteMap };
        }
        
        this.currentPage = this.getCurrentPage();
        this.dynamicTitle = null;
        this.dynamicSubtitle = null;
    }
    
    getCurrentPage() {
        const path = window.location.pathname;
        const filename = path.split('/').pop() || 'index.html';
        return filename === '' ? 'index.html' : filename;
    }
    
    /**
     * Set dynamic title for pages like object detail
     * @param {string} title - The dynamic title
     * @param {string} subtitle - Optional subtitle
     */
    setDynamicTitle(title, subtitle = null) {
        this.dynamicTitle = title;
        this.dynamicSubtitle = subtitle;
        this.render();
    }
    
    /**
     * Generate breadcrumb trail for current page
     * @returns {Array} Array of breadcrumb items
     */
    generateBreadcrumbs() {
        const breadcrumbs = [];
        const pageConfig = this.siteMap[this.currentPage];
        
        if (!pageConfig) {
            // Fallback for unknown pages
            if (this.showHome) {
                breadcrumbs.push({
                    title: this.homeTitle,
                    url: 'index.html',
                    icon: '🏠',
                    active: false
                });
            }
            breadcrumbs.push({
                title: document.title || 'Unknown Page',
                url: null,
                active: true
            });
            return breadcrumbs;
        }
        
        // Build breadcrumb chain
        this.buildBreadcrumbChain(pageConfig, breadcrumbs);
        
        // Add current page (always last and active)
        const currentTitle = pageConfig.dynamic && this.dynamicTitle ? 
            this.dynamicTitle : pageConfig.title;
            
        breadcrumbs.push({
            title: currentTitle,
            subtitle: this.dynamicSubtitle,
            url: null,
            icon: pageConfig.icon,
            active: true
        });
        
        return breadcrumbs;
    }
    
    buildBreadcrumbChain(pageConfig, breadcrumbs) {
        // Always start with home if showHome is true and not already on home page
        if (this.showHome && this.currentPage !== 'index.html') {
            const homeConfig = this.siteMap['index.html'];
            breadcrumbs.push({
                title: homeConfig.title,
                url: 'index.html',
                icon: homeConfig.icon,
                active: false
            });
        }
        
        // Handle parent relationships
        if (pageConfig.parent) {
            if (pageConfig.parent.endsWith('.html')) {
                // Parent is a page
                const parentConfig = this.siteMap[pageConfig.parent];
                if (parentConfig && pageConfig.parent !== 'index.html') {
                    // Only add parent if it's not already home (avoid duplication)
                    if (breadcrumbs.length === 0 || breadcrumbs[breadcrumbs.length - 1]?.url !== pageConfig.parent) {
                        breadcrumbs.push({
                            title: parentConfig.title,
                            url: pageConfig.parent,
                            icon: parentConfig.icon,
                            active: false
                        });
                    }
                }
            } else {
                // Parent is a section (like 'nircam')
                const parentIcon = pageConfig.parent === 'nircam' ? '📷' : null;
                breadcrumbs.push({
                    title: pageConfig.parentTitle || pageConfig.parent,
                    url: pageConfig.parentUrl || '#',
                    icon: parentIcon,
                    active: false
                });
            }
        }
    }
    
    /**
     * Render breadcrumbs to DOM
     */
    render() {
        const container = document.getElementById(this.containerId);
        if (!container) {
            console.warn(`BreadcrumbManager: Container #${this.containerId} not found`);
            return;
        }
        
        const breadcrumbs = this.generateBreadcrumbs();
        
        if (breadcrumbs.length <= 1) {
            // Don't show breadcrumbs if there's only home or current page
            container.style.display = 'none';
            return;
        }
        
        container.style.display = 'flex';
        container.innerHTML = this.renderBreadcrumbHTML(breadcrumbs);
        
        // Add click handlers for non-active breadcrumbs
        this.addEventListeners();
    }
    
    renderBreadcrumbHTML(breadcrumbs) {
        return breadcrumbs.map((crumb, index) => {
            const isLast = index === breadcrumbs.length - 1;
            const separator = !isLast ? `<span class="breadcrumb-separator">${this.separator}</span>` : '';
            
            let crumbHTML = '';
            
            if (crumb.active || !crumb.url || crumb.url === '#') {
                // Active or non-clickable crumb
                crumbHTML = `
                    <span class="breadcrumb-item ${crumb.active ? 'active' : 'non-clickable'}">
                        ${crumb.icon ? `<span class="breadcrumb-icon">${crumb.icon}</span>` : ''}
                        <span class="breadcrumb-title">${crumb.title}</span>
                        ${crumb.subtitle ? `<span class="breadcrumb-subtitle">${crumb.subtitle}</span>` : ''}
                    </span>
                `;
            } else {
                // Clickable crumb
                crumbHTML = `
                    <a href="${crumb.url}" class="breadcrumb-item clickable">
                        ${crumb.icon ? `<span class="breadcrumb-icon">${crumb.icon}</span>` : ''}
                        <span class="breadcrumb-title">${crumb.title}</span>
                    </a>
                `;
            }
            
            return crumbHTML + separator;
        }).join('');
    }
    
    addEventListeners() {
        const clickableCrumbs = document.querySelectorAll('.breadcrumb-item.clickable');
        clickableCrumbs.forEach(crumb => {
            crumb.addEventListener('click', (e) => {
                // Allow default link behavior, but could add analytics here
                console.log('Breadcrumb navigation:', crumb.href);
            });
        });
    }
    
    /**
     * Update breadcrumb for dynamic content
     * Useful for pages that change content without navigation
     */
    update(title, subtitle = null) {
        this.setDynamicTitle(title, subtitle);
    }
    
    /**
     * Initialize breadcrumbs for current page
     * Call this after DOM is ready
     */
    init() {
        this.render();
    }
    
    /**
     * Get breadcrumb data for SEO structured data
     * @returns {Array} Structured data for breadcrumbs
     */
    getStructuredData() {
        const breadcrumbs = this.generateBreadcrumbs();
        
        return {
            '@context': 'https://schema.org',
            '@type': 'BreadcrumbList',
            'itemListElement': breadcrumbs.map((crumb, index) => ({
                '@type': 'ListItem',
                'position': index + 1,
                'name': crumb.title,
                'item': crumb.url ? `${window.location.origin}/${crumb.url}` : window.location.href
            }))
        };
    }
}

// Global breadcrumb manager instance
window.BreadcrumbManager = BreadcrumbManager;

// Auto-initialize if container exists
document.addEventListener('DOMContentLoaded', () => {
    // Only auto-initialize if no custom initialization is detected
    if (document.getElementById('breadcrumbNav') && !window.breadcrumbManager) {
        window.breadcrumbManager = new BreadcrumbManager();
        window.breadcrumbManager.init();
    }
});