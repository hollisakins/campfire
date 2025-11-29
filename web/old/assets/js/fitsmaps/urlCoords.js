const is_ra_dec = 1;
const crpix = [29917.801194458, 29596.789412419];
const crval = [150.11632752531, 2.2009681511549];
const cdmatrix = [[-1.6666666845192065e-05, -2.8356365655772694e-13], [-1.9895196601282805e-13, 1.6666666661002694e-05]];

urlParam = function(name){
    // Parse parameters from window.location,,
    // e.g., .../index.html?zoom=8,
    // urlParam(zoom) = 8,
    var results = new RegExp('[\?&]' + name + '=([^&#]*)').exec(window.location.href);
    if (results==null){
        return null;
    }
    else{
        return decodeURI(results[1]) || 0;
    }
}

pixToSky = function(xy){
    // Convert from zero-index pixel to sky coordinate assuming
    // simple North-up WCS
    if (xy.hasOwnProperty('lng')){
        var dx = xy.lng - crpix[0] + 1;
        var dy = xy.lat - crpix[1] + 1;
    } else {
        var dx = xy[0] - crpix[0] + 1;
        var dy = xy[1] - crpix[1] + 1;
    }
    var dra = dx * cdmatrix[0][0];
    var ddec = dy * cdmatrix[1][1];
    // some catalogs are stored in image coords x/y, not ra/dec. When
    // `is_ra_dec`==1 we are doing calculation in ra/dec when `is_ra_dec`==0
    // then we're working in image coords and so multiply by 0 so
    // cos(0)==1
    var ra = crval[0] + dra / Math.cos(crval[1]/180*3.14159 * is_ra_dec);
    var dec = crval[1] + ddec;
    return [ra, dec];
}

skyToPix = function(rd){
    // Convert from sky to zero-index pixel coordinate assuming
    // simple North-up WCS
    var dx = (rd[0] - crval[0]) * Math.cos(crval[1]/180*3.14159 * is_ra_dec);
    var dy = (rd[1] - crval[1]);
    var x = crpix[0] - 1 + dx / cdmatrix[0][0];
    var y = crpix[1] - 1 + dy / cdmatrix[1][1];
    return [x,y];
}

skyToLatLng = function(rd){
    // Convert from sky to Leaflet.latLng coordinate assuming
    // simple North-up WCS
    var xy = skyToPix(rd);
    return L.latLng(xy[1], xy[0]);
}

panToSky = function(rd, zoom, map){
    // Pan map to celestial coordinates
    var ll = skyToLatLng(rd)
    map.setZoom(zoom);
    map.panTo(ll, zoom);
    //console.log('pan to: ' + rd + ' / ll: ' + ll.lng + ',' + ll.lat);
}

panFromUrl = function(map){
    // Pan map based on ra/dec/[zoom] variables in location bar
    var ra = urlParam('ra');
    var dec = urlParam('dec');
    var zoom = urlParam('zoom') || map.getMinZoom();
    if ((ra !== null) & (dec !== null)) {
        panToSky([ra,dec], zoom, map);
    } else {
        // Pan to crval
        panToSky(crval, zoom, map);
    }
}

parseCoord = function(rd, hours) {
    // Parse sexagesimal coordinates if ':' found in rd
    if (rd.includes(':')){
        var dms = rd.split(':')
        
        var deg = dms[0]*1; 
        if (deg < 0) {
            var sign = -1;
        } else {
            var sign = 1;
        }
        deg += sign*dms[1]/60. + sign*dms[2]/3600.;
        if (hours > 0) {
            deg *= 360/24.
        }
    } else {
        var deg = rd;
    }
    return deg
}


panFromBox = function (map) {
    // Pan map based on ra/dec/[zoom] variables in coordinate box
    var coord = document.getElementById('boxtext').value;
    var rd = coord.split(',');
    if ((rd.length == 1)) {
        rd = coord.split(' ');
    }
    if ((rd.length == 3)) {
        zoom = rd[2];
    } else {
        zoom = map.getZoom();
    }

    console.log(rd);
    panToSky([parseCoord(rd[0], 1), parseCoord(rd[1], 0)], zoom, map);
}

updateLocationBar = function(){
    var rd = pixToSky(map.getCenter());
    //console.log(rd);
    var params = 'ra=' + rd[0].toFixed(7);
    params += '&dec=' + rd[1].toFixed(7);
    params += '&zoom=' + map.getZoom();
    //console.log(params);
    var param_url = window.location.href.split('?')[0] + '?' + params;
    window.history.pushState('', '', param_url);
}

L.Control.CoordSearch = L.Control.extend({
    options : {
        default : 'RA Dec',
    },
    
    makeIcon: function() {
        let iconDiv = '<div id="coordsearch-control-icon" class="coordsearch-control menu-button">'
        iconDiv += `<?xml version="1.0" encoding="utf-8"?><!-- Uploaded to: SVG Repo, www.svgrepo.com, Generator: SVG Repo Mixer Tools -->
            <svg width="30px" height="30px"  viewBox="1.5 1.5 26.5 27" fill="none" xmlns="http://www.w3.org/2000/svg">
                <image xlink:href="https://www.svgrepo.com/download/532236/crosshair.svg" width:"30px" height="30px"/>
            </svg>`;

        iconDiv += '</div>';
        return iconDiv;
    },    
    
    onAdd: function (map) {
        const menuDiv = L.DomUtil.create('div', 'coordsearch-control');

        // This is the menu icon as an SVG
        let iconDiv = this.makeIcon();

        let menuHTML = '<div id="coordsearch-control-menu" class="coordsearch-control menu-control collapsed">' 
        menuHTML += '<input type="text" placeholder="' 
        menuHTML += this.options.default
        menuHTML += '" id="boxtext" class="input"/>';
        // menuHTML += '" id="boxtext" class="input" onkeydown="panFromBox(map)"/>';
        menuHTML += "</div>";

        iconDiv += menuHTML;
        menuDiv.innerHTML = iconDiv;

        menuDiv.id = "coordsearch-control";
        
        L.DomEvent.disableClickPropagation(menuDiv);

        L.DomEvent.on(menuDiv, {
            mouseenter: this.expand,
            mouseleave: this.collapse
        }, this);

        return menuDiv;
    },

    moveBadge: function (text) {
        const parent = document.getElementById("coordsearch-control");
        const range = document.getElementById(text);
        const badge = document.getElementById(text + "-badge");
        badge.innerHTML = range.value;
        badge.style.visibility = "visible";
        const offsetLeft = range.getBoundingClientRect().left - parent.getBoundingClientRect().left;
        badge.style.left = offsetLeft - 4 + (range.value / range.max * 0.9) * range.getBoundingClientRect().width + "px";
        const offsetTop = range.getBoundingClientRect().top - parent.getBoundingClientRect().top;
        badge.style.top = offsetTop - 22 + "px";
    },

    hideBadge: function (text) {
        const badge = document.getElementById(text + "-badge");
        badge.style.visibility = "hidden";
    },

    onRemove: function (map) {
    },

	// @method expand(): this
	// Expand the control container if collapsed.
	expand() {
        // this.update_catalog_colorpickers(this.options.catalogs);
        document.getElementById("coordsearch-control-menu").classList.remove("collapsed");
        document.getElementById("coordsearch-control-icon").classList.add("collapsed");
	},

	// @method collapse(): this
	// Collapse the control container if expanded.
	collapse() {
        document.getElementById("coordsearch-control-icon").classList.remove("collapsed");
        document.getElementById("coordsearch-control-menu").classList.add("collapsed");
	},

    enable() {
        // Enable the control
        var input = document.getElementById("boxtext");

        // Execute a function when the user presses a key on the keyboard
        input.addEventListener("keypress", function(event) {
            // If the user presses the "Enter" key on the keyboard
            if (event.key === "Enter") {
                event.preventDefault(); // Prevents the default action of the Enter key
                panFromBox(map);
            }
        }); 
    }

    
});

L.control.coordsearch = function (opts) {
    const coordsearch = new L.Control.CoordSearch(opts);

    // Add the CSS for the settings control for the img css filters
    const imgStyle = document.createElement("style");
    imgStyle.id = "img-filters";
    document.getElementsByTagName("head")[0].appendChild(imgStyle);    


    return coordsearch;
}
