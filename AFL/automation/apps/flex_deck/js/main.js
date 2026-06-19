/* Flex Deck Visualizer — main.js */

var token = null;

async function login() {
    if (token) return token;
    const response = await fetch('/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username:'dashboard', password:'domo_arigato'})
    });
    if (!response.ok) { alert('Login failed'); throw new Error('login'); }
    const data = await response.json();
    token = data.token;
    return token;
}

// Clear the cached token and show an error alert.
// On 401 the token has expired; clearing it forces re-login on the next action.
function ajaxError(xhr, msg) {
    if (xhr.status === 401) { token = null; }
    alert((msg || 'Error') + ': ' + xhr.responseText);
}

const labwareChoices = (window.OT2DeckData && window.OT2DeckData.labwareChoices) || {};

var _MODULE_NAMES = ['heaterShakerModuleV1', 'thermocyclerModuleV2', 'magneticBlockV1', 'temperatureModuleV2', 'absorbanceReaderV1'];
var _STAGING_SLOTS = ['A4', 'B4', 'C4', 'D4'];

function showLabwareOptions(slot) {
    var isStaging = _STAGING_SLOTS.indexOf(slot) !== -1;
    var select = $('<select></select>');
    Object.entries(labwareChoices).forEach(function(entry) {
        var key = entry[0];
        var value = entry[1];
        // Staging slots can only hold labware, not modules
        if (isStaging && _MODULE_NAMES.indexOf(key) !== -1) return;
        select.append($('<option>').attr('value', key).text(value));
    });
    var title = isStaging
        ? 'Load labware in staging slot ' + slot + ' (gripper only)'
        : 'Load labware or module in slot ' + slot;
    $('<div></div>').append(select).dialog({
        title: title,
        modal: true,
        buttons: {
            'Load': function() {
                var lw = select.val();
                var task = _MODULE_NAMES.indexOf(lw) !== -1 ? 'load_module' : 'load_labware';
                login().then(function(tok) {
                    $.ajax({
                        type: 'POST',
                        url: '/enqueue',
                        headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
                        data: JSON.stringify({task_name: task, name: lw, slot: slot}),
                        success: function() { setTimeout(function() { location.reload(); }, 500); },
                        error: function(xhr) { ajaxError(xhr); }
                    });
                }).catch(function(e) { console.error('Load labware failed:', e); });
                $(this).dialog('destroy').remove();
            },
            'Cancel': function() { $(this).dialog('destroy').remove(); }
        }
    });
}

function resetTipracks(mount) {
    login().then(function(tok) {
        $.ajax({
            type: 'POST',
            url: '/enqueue',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
            data: JSON.stringify({task_name: 'reset_tipracks', mount: mount}),
            success: function() { location.reload(); },
            error: function(xhr) { ajaxError(xhr); }
        });
    }).catch(function(e) { console.error('Reset tipracks failed:', e); });
}

function loadGripper() {
    login().then(function(tok) {
        $.ajax({
            type: 'POST',
            url: '/enqueue',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
            data: JSON.stringify({task_name: 'load_gripper'}),
            success: function() { location.reload(); },
            error: function(xhr) { ajaxError(xhr, 'Error loading gripper'); }
        });
    }).catch(function(e) { console.error('Load gripper failed:', e); });
}

function openMoveLabwareDialog(sourceSlot, labwareName) {
    var data = window.OT2DeckData || {};
    var allSlots = (data.allSlots || []).filter(function(s) { return s !== sourceSlot; });
    var gripperLoaded = data.gripperLoaded || false;

    var destSelect = $('<select id="move-dest-select"></select>');
    allSlots.forEach(function(s) {
        var label = isNaN(parseInt(s)) ? 'Slot ' + s : 'Slot ' + s;
        destSelect.append($('<option>').val(s).text(label));
    });
    destSelect.append($('<option>').val('offDeck').text('Off Deck (remove from deck)'));

    var gripperCheck = $(
        '<label style="display:block;margin-top:10px;">'
        + '<input type="checkbox" id="use-gripper-check"' + (gripperLoaded ? ' checked' : '') + '> '
        + 'Use gripper</label>'
    );

    var content = $('<div></div>')
        .append($('<p style="margin:0 0 8px 0;">').html(
            '<b>Moving:</b> ' + $('<span>').text(labwareName).html()
            + ' &nbsp;(slot ' + sourceSlot + ') &rarr;'
        ))
        .append(destSelect)
        .append(gripperCheck);

    if (!gripperLoaded) {
        content.append($('<p class="move-warning">'
            + '&#9888; No gripper loaded — move will pause for manual repositioning.</p>'));
    }

    content.dialog({
        title: 'Move Labware',
        modal: true,
        width: 360,
        buttons: {
            'Move': function() {
                var dest = $('#move-dest-select').val();
                var useGripper = $('#use-gripper-check').prop('checked');
                moveLabware(sourceSlot, dest, useGripper);
                $(this).dialog('destroy').remove();
            },
            'Cancel': function() { $(this).dialog('destroy').remove(); }
        }
    });
}

function moveLabware(sourceSlot, destSlot, useGripper) {
    login().then(function(tok) {
        $.ajax({
            type: 'POST',
            url: '/enqueue',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
            data: JSON.stringify({task_name: 'move_labware', source_slot: sourceSlot, dest_slot: destSlot, use_gripper: useGripper}),
            success: function() { setTimeout(function() { location.reload(); }, 800); },
            error: function(xhr) { ajaxError(xhr, 'Move failed'); }
        });
    }).catch(function(e) { console.error('Move labware failed:', e); });
}

function openPrepTargetDialog(slot, targets) {
    var wells = targets.split(',').map(function(t) { return t.slice(2); });
    var rows = [];
    var cols = [];
    wells.forEach(function(w) {
        var m = w.match(/([A-Za-z]+)(\d+)/);
        if (!m) return;
        if (rows.indexOf(m[1]) === -1) rows.push(m[1]);
        var c = parseInt(m[2]);
        if (cols.indexOf(c) === -1) cols.push(c);
    });
    rows.sort();
    cols.sort(function(a, b) { return a - b; });
    var table = $('<table class="well-select-table"></table>');
    var header = $('<tr><th></th></tr>');
    cols.forEach(function(c) { header.append('<th class="col-header" data-col="' + c + '">' + c + '</th>'); });
    table.append(header);
    rows.forEach(function(r) {
        var row = $('<tr></tr>');
        row.append('<th class="row-header" data-row="' + r + '">' + r + '</th>');
        cols.forEach(function(c) {
            var cell = $('<td class="well-cell" data-row="' + r + '" data-col="' + c + '" data-well="' + slot + r + c + '"></td>');
            row.append(cell);
        });
        table.append(row);
    });
    table.on('click', '.well-cell', function() { $(this).toggleClass('selected'); });
    table.on('click', '.row-header', function() {
        var r = $(this).data('row');
        var cells = table.find('.well-cell[data-row="' + r + '"]');
        var sel = cells.filter('.selected').length === cells.length;
        cells.toggleClass('selected', !sel);
    });
    table.on('click', '.col-header', function() {
        var c = $(this).data('col');
        var cells = table.find('.well-cell[data-col="' + c + '"]');
        var sel = cells.filter('.selected').length === cells.length;
        cells.toggleClass('selected', !sel);
    });
    var controls = $('<div style="text-align:center;margin-bottom:6px;"></div>');
    var selectAll = $('<button>Select All</button>').click(function() {
        table.find('.well-cell').addClass('selected');
    });
    var deselectAll = $('<button>Deselect All</button>').click(function() {
        table.find('.well-cell').removeClass('selected');
    });
    controls.append(selectAll).append(' ').append(deselectAll);
    var dialog = $('<div></div>').append(controls).append(table).dialog({
        title: 'Manage Prep Targets',
        modal: true,
        width: 'auto',
        buttons: {
            'Append': function() {
                var list = table.find('.well-cell.selected').map(function() { return $(this).data('well'); }).get();
                if (list.length === 0) { alert('Select at least one well'); return; }
                appendPrepTargets(list.join(','));
                dialog.dialog('destroy').remove();
            },
            'Redefine': function() {
                var list = table.find('.well-cell.selected').map(function() { return $(this).data('well'); }).get();
                if (list.length === 0) { alert('Select at least one well'); return; }
                setPrepTargets(list.join(','));
                dialog.dialog('destroy').remove();
            },
            'Cancel': function() { dialog.dialog('destroy').remove(); }
        }
    });
}

function appendPrepTargets(targets) {
    var t = targets.split(',');
    login().then(function(tok) {
        $.ajax({
            type: 'POST',
            url: '/enqueue',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
            data: JSON.stringify({task_name: 'add_prep_targets', targets: t, reset: false}),
            success: function() { location.reload(); },
            error: function(xhr) { ajaxError(xhr); }
        });
    }).catch(function(e) { console.error('Append prep targets failed:', e); });
}

function setPrepTargets(targets) {
    var t = targets.split(',');
    login().then(function(tok) {
        $.ajax({
            type: 'POST',
            url: '/enqueue',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
            data: JSON.stringify({task_name: 'add_prep_targets', targets: t, reset: true}),
            success: function() { location.reload(); },
            error: function(xhr) { ajaxError(xhr); }
        });
    }).catch(function(e) { console.error('Set prep targets failed:', e); });
}

$(document).ready(function() {
    $('#load-instrument-btn').click(function() {
        var mount = $('#mount-select').val();
        var pipette = $('#pipette-select').val();
        var tipracks = $('#tiprack-slots').val().split(',').map(function(x) { return x.trim(); }).filter(Boolean);
        if (!mount || !pipette) { alert('Select mount and pipette.'); return; }
        login().then(function(tok) {
            $.ajax({
                type: 'POST',
                url: '/enqueue',
                headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
                data: JSON.stringify({task_name: 'load_instrument', mount: mount, name: pipette, tip_rack_slots: tipracks}),
                success: function() { location.reload(); },
                error: function(xhr) { ajaxError(xhr); }
            });
        }).catch(function(e) { console.error('Load instrument failed:', e); });
    });

    $('#reset-deck-btn').click(function() {
        if (!confirm('Are you sure you want to reset the entire deck?')) return;
        login().then(function(tok) {
            $.ajax({
                type: 'POST',
                url: '/enqueue',
                headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
                data: JSON.stringify({task_name: 'reset_deck'}),
                success: function() { location.reload(); },
                error: function(xhr) { ajaxError(xhr); }
            });
        }).catch(function(e) { console.error('Reset deck failed:', e); });
    });

    // Staging area toggle — show/hide column and persist via set_staging_areas
    var stagingEnabled = window.OT2DeckData && window.OT2DeckData.stagingEnabled;
    var stagingCol = $('#staging-column');
    var toggleBtn = $('#toggle-staging-btn');

    function updateToggleBtn(enabled) {
        toggleBtn.css({
            background: enabled ? '#388e3c' : '#757575',
            color: 'white',
            fontWeight: 'bold'
        }).text(enabled ? '\u25A3 Staging Area: ON' : '\u25A2 Staging Area: OFF');
    }
    updateToggleBtn(stagingEnabled);

    toggleBtn.click(function() {
        stagingEnabled = !stagingEnabled;
        stagingCol.toggle(stagingEnabled);
        updateToggleBtn(stagingEnabled);
        var cutouts = stagingEnabled ? ['cutoutB3', 'cutoutC3', 'cutoutD3'] : [];
        login().then(function(tok) {
            $.ajax({
                type: 'POST',
                url: '/query_driver',
                headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tok},
                data: JSON.stringify({task_name: 'set_staging_areas', cutouts: cutouts}),
                error: function(xhr) { if (xhr.status === 401) { token = null; } console.warn('set_staging_areas failed:', xhr.responseText); }
            });
        }).catch(function(e) { console.error('Set staging areas failed:', e); });
    });

    // ------------------------------------------------------------------
    // Deck camera auto-refresh
    // ------------------------------------------------------------------
    var _cameraInterval = null;
    var CAMERA_INTERVAL_MS = 5000;

    function refreshCamera() {
        var img = document.getElementById('deck-camera-img');
        if (img) {
            // Cache-bust so the browser doesn't serve the previous frame.
            img.src = '/query_driver?task_name=get_snapshot&_t=' + Date.now();
        }
    }

    function startCameraRefresh() {
        if (_cameraInterval) return;
        _cameraInterval = setInterval(refreshCamera, CAMERA_INTERVAL_MS);
    }

    function toggleCameraRefresh() {
        var btn = document.getElementById('camera-pause-btn');
        if (_cameraInterval) {
            clearInterval(_cameraInterval);
            _cameraInterval = null;
            if (btn) btn.textContent = 'Resume';
        } else {
            startCameraRefresh();
            if (btn) btn.textContent = 'Pause';
        }
    }

    // Start auto-refresh if the camera panel is present in the page.
    if (document.getElementById('deck-camera-img')) {
        startCameraRefresh();
    }
});

