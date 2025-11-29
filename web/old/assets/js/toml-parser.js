/**
 * Minimal TOML Parser for DATA_MANIFEST files
 * Supports the subset of TOML features needed for this project
 */

const TOML = (function() {
    'use strict';

    function parse(input) {
        const lines = input.split('\n');
        const result = {};
        let currentSection = result;
        let currentArray = null;
        let currentArrayName = null;
        
        for (let i = 0; i < lines.length; i++) {
            let line = lines[i].trim();
            
            // Skip empty lines and comments
            if (!line || line.startsWith('#')) continue;
            
            // Handle sections [section]
            if (line.startsWith('[') && line.endsWith(']') && !line.startsWith('[[')) {
                const sectionName = line.slice(1, -1).trim();
                result[sectionName] = {};
                currentSection = result[sectionName];
                currentArray = null;
                continue;
            }
            
            // Handle array of tables [[array]]
            if (line.startsWith('[[') && line.endsWith(']]')) {
                const arrayName = line.slice(2, -2).trim();
                if (!result[arrayName]) {
                    result[arrayName] = [];
                }
                const newItem = {};
                result[arrayName].push(newItem);
                currentSection = newItem;
                currentArray = result[arrayName];
                currentArrayName = arrayName;
                continue;
            }
            
            // Handle key = value pairs
            const equalIndex = line.indexOf('=');
            if (equalIndex > 0) {
                const key = line.substring(0, equalIndex).trim();
                let value = line.substring(equalIndex + 1).trim();
                
                // Strip inline comments
                const commentIndex = value.indexOf('#');
                if (commentIndex >= 0) {
                    value = value.substring(0, commentIndex).trim();
                }
                
                // Parse the value
                if ((value.startsWith('"') && value.endsWith('"')) || 
                    (value.startsWith("'") && value.endsWith("'"))) {
                    // String (handle both single and double quotes)
                    value = value.slice(1, -1);
                    // Handle escaped quotes
                    value = value.replace(/\\"/g, '"').replace(/\\'/g, "'");
                } else if (value === 'true') {
                    value = true;
                } else if (value === 'false') {
                    value = false;
                } else if (!isNaN(value) && value !== '') {
                    // Number
                    value = Number(value);
                }
                
                currentSection[key] = value;
            }
        }
        
        return result;
    }
    
    return {
        parse: parse
    };
})();