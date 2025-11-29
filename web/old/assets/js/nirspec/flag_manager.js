/**
 * FlagManager - Handle bitmask operations and flag definitions for NIRSpec inspection system
 * 
 * This class provides utilities for:
 * - Loading flag configuration from JSON
 * - Encoding/decoding bitmasks
 * - Converting between old and new flag systems
 * - Displaying flags with proper styling
 */

class FlagManager {
    constructor() {
        this.config = null;
        this.initialized = false;
        this.loadPromise = null;
    }

    /**
     * Initialize the flag manager by loading configuration
     * @returns {Promise} Promise that resolves when configuration is loaded
     */
    async initialize() {
        if (this.initialized) return;
        if (this.loadPromise) return this.loadPromise;

        this.loadPromise = this.loadConfig();
        await this.loadPromise;
        this.initialized = true;
    }

    /**
     * Load flag configuration from JSON file
     * @returns {Promise}
     */
    async loadConfig() {
        try {
            const response = await fetch('/static/config/inspection_flags.json');
            if (!response.ok) {
                throw new Error(`Failed to load flag config: ${response.status}`);
            }
            this.config = await response.json();
            console.log('Flag configuration loaded:', this.config.metadata);
        } catch (error) {
            console.error('Failed to load flag configuration:', error);
            throw error;
        }
    }

    /**
     * Get redshift quality information for a given value
     * @param {number} value - Quality value (0-4)
     * @returns {Object} Quality definition with label, icon, color, etc.
     */
    getQualityInfo(value) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const quality = this.config.redshift_quality[value];
        return quality || {
            value: value,
            label: 'Unknown',
            short: 'UNK',
            icon: '❓',
            color: '#666666',
            description: `Unknown quality value: ${value}`
        };
    }

    /**
     * Get all available quality options
     * @returns {Array} Array of quality definitions
     */
    getQualityOptions() {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        return Object.values(this.config.redshift_quality).sort((a, b) => a.value - b.value);
    }

    /**
     * Encode multiple flags into a bitmask for a given category
     * @param {string} category - Flag category (e.g., 'object_flags')
     * @param {Array<string>} flagKeys - Array of flag keys to encode
     * @returns {number} Bitmask value
     */
    encodeBitmask(category, flagKeys) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const categoryConfig = this.config.flag_categories[category];
        if (!categoryConfig) {
            throw new Error(`Unknown flag category: ${category}`);
        }

        let bitmask = 0;
        const flags = categoryConfig.flags;

        for (const flagKey of flagKeys) {
            if (flags[flagKey]) {
                bitmask |= flags[flagKey].value;
            } else {
                console.warn(`Unknown flag '${flagKey}' in category '${category}'`);
            }
        }

        return bitmask;
    }

    /**
     * Decode a bitmask into an array of flag keys
     * @param {string} category - Flag category
     * @param {number} bitmask - Bitmask to decode
     * @returns {Array<string>} Array of flag keys
     */
    decodeBitmask(category, bitmask) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const categoryConfig = this.config.flag_categories[category];
        if (!categoryConfig) {
            throw new Error(`Unknown flag category: ${category}`);
        }

        const flagKeys = [];
        const flags = categoryConfig.flags;

        for (const [flagKey, flagDef] of Object.entries(flags)) {
            if (bitmask & flagDef.value) {
                flagKeys.push(flagKey);
            }
        }

        return flagKeys;
    }

    /**
     * Get flag definition for a specific flag
     * @param {string} category - Flag category
     * @param {string} flagKey - Flag key
     * @returns {Object} Flag definition
     */
    getFlagDefinition(category, flagKey) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const categoryConfig = this.config.flag_categories[category];
        if (!categoryConfig) {
            throw new Error(`Unknown flag category: ${category}`);
        }

        const flagDef = categoryConfig.flags[flagKey];
        if (!flagDef) {
            throw new Error(`Unknown flag '${flagKey}' in category '${category}'`);
        }

        return flagDef;
    }

    /**
     * Get all flag definitions for a category
     * @param {string} category - Flag category
     * @returns {Object} Object with flag definitions
     */
    getCategoryFlags(category) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const categoryConfig = this.config.flag_categories[category];
        if (!categoryConfig) {
            throw new Error(`Unknown flag category: ${category}`);
        }

        return categoryConfig.flags;
    }

    /**
     * Get category information
     * @param {string} category - Flag category
     * @returns {Object} Category configuration
     */
    getCategoryInfo(category) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        return this.config.flag_categories[category];
    }

    /**
     * Convert old quality value to new scale
     * @param {string|number} oldValue - Old quality value (-1, 0, 1, 2)
     * @returns {number} New quality value (0-4)
     */
    migrateQualityValue(oldValue) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const migrationMap = this.config.migration?.redshift_quality_map || {};
        const oldStr = String(oldValue);
        
        if (oldStr in migrationMap) {
            return migrationMap[oldStr];
        }

        // Try direct conversion if it's already a valid new value
        const numValue = parseInt(oldValue);
        if (numValue >= 0 && numValue <= 4 && this.config.redshift_quality[numValue]) {
            return numValue;
        }

        // Default fallback
        return 0; // Not inspected
    }

    /**
     * Convert old string flags to new flag keys
     * @param {string} category - Flag category
     * @param {Array<string>} oldFlags - Array of old string flags
     * @returns {Array<string>} Array of new flag keys
     */
    migrateFlags(category, oldFlags) {
        if (!this.config) throw new Error('FlagManager not initialized');
        
        const migrationMap = this.config.migration?.[`${category}_map`] || {};
        const newFlags = [];

        for (const oldFlag of oldFlags) {
            if (migrationMap[oldFlag]) {
                newFlags.push(migrationMap[oldFlag]);
            } else {
                // Try direct match
                const categoryFlags = this.getCategoryFlags(category);
                if (categoryFlags[oldFlag]) {
                    newFlags.push(oldFlag);
                } else {
                    console.warn(`No migration mapping for '${oldFlag}' in '${category}'`);
                }
            }
        }

        return newFlags;
    }

    /**
     * Create HTML element for displaying a flag
     * @param {string} category - Flag category
     * @param {string} flagKey - Flag key
     * @param {Object} options - Display options
     * @returns {HTMLElement} Flag chip element
     */
    createFlagChip(category, flagKey, options = {}) {
        const flagDef = this.getFlagDefinition(category, flagKey);
        
        const chip = document.createElement('span');
        chip.className = `flag-chip ${options.className || ''}`;
        chip.style.backgroundColor = flagDef.color || '#e0e0e0';
        chip.title = flagDef.description || flagDef.label;
        chip.dataset.category = category;
        chip.dataset.flag = flagKey;

        const content = [];
        if (flagDef.icon && options.showIcon !== false) {
            content.push(`<span class="flag-icon">${flagDef.icon}</span>`);
        }
        if (options.showLabel !== false) {
            const label = options.useShort ? (flagDef.short || flagDef.label) : flagDef.label;
            content.push(`<span class="flag-label">${label}</span>`);
        }

        chip.innerHTML = content.join('');
        return chip;
    }

    /**
     * Create HTML elements for displaying multiple flags from a bitmask
     * @param {string} category - Flag category
     * @param {number} bitmask - Bitmask value
     * @param {Object} options - Display options
     * @returns {Array<HTMLElement>} Array of flag chip elements
     */
    createFlagChipsFromBitmask(category, bitmask, options = {}) {
        const flagKeys = this.decodeBitmask(category, bitmask);
        return flagKeys.map(flagKey => this.createFlagChip(category, flagKey, options));
    }

    /**
     * Create quality badge element
     * @param {number} qualityValue - Quality value (0-4)
     * @param {Object} options - Display options
     * @returns {HTMLElement} Quality badge element
     */
    createQualityBadge(qualityValue, options = {}) {
        const quality = this.getQualityInfo(qualityValue);
        
        const badge = document.createElement('span');
        badge.className = `quality-badge ${options.className || ''}`;
        badge.style.backgroundColor = quality.color;
        badge.style.color = this.getContrastColor(quality.color);
        badge.title = quality.description;
        badge.dataset.quality = qualityValue;

        const content = [];
        if (quality.icon && options.showIcon !== false) {
            content.push(quality.icon);
        }
        if (options.showLabel !== false) {
            const label = options.useShort ? quality.short : quality.label;
            content.push(label);
        }

        badge.textContent = content.join(' ');
        return badge;
    }

    /**
     * Get contrasting text color for a background color
     * @param {string} bgColor - Background color in hex format
     * @returns {string} Either 'black' or 'white'
     */
    getContrastColor(bgColor) {
        // Remove # if present
        const hex = bgColor.replace('#', '');
        
        // Convert to RGB
        const r = parseInt(hex.substr(0, 2), 16);
        const g = parseInt(hex.substr(2, 2), 16);
        const b = parseInt(hex.substr(4, 2), 16);
        
        // Calculate luminance
        const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
        
        return luminance > 0.5 ? 'black' : 'white';
    }

    /**
     * Get all bitmask categories
     * @returns {Array<string>} Array of category names
     */
    getBitmaskCategories() {
        if (!this.config) throw new Error('FlagManager not initialized');
        return this.config.bitmask_categories || [];
    }

    /**
     * Check if a category uses bitmask storage
     * @param {string} category - Category name
     * @returns {boolean} True if category uses bitmask
     */
    isBitmaskCategory(category) {
        return this.getBitmaskCategories().includes(category);
    }
}

// Create global instance
window.flagManager = new FlagManager();

// Auto-initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
    window.flagManager.initialize().catch(error => {
        console.error('Failed to initialize flag manager:', error);
    });
});