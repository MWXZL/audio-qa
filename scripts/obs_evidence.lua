obs = obslua

local command_file = ""
local state_file = ""
local last_id = ""
local active_id = ""
local pending = nil
local initialized = false
local targets = {}

local function await_result(command, previous)
    pending = {command = command, previous = previous or "", started = os.time()}
end

local function poll_result()
    if pending == nil then return false end
    local command = pending.command
    local file = ""
    local status = nil
    if command == "start" and obs.obs_frontend_recording_active() then
        status = "recording"
    elseif command == "stop" and not obs.obs_frontend_recording_active() then
        file = obs.obs_frontend_get_last_recording()
        if os.time() - pending.started >= 1 then status = "stopped" end
    elseif command == "screenshot" then
        file = obs.obs_frontend_get_last_screenshot()
        if file ~= "" and file ~= pending.previous then status = "screenshot" end
    end
    if status ~= nil then
        pending = nil
        return status, file
    end
    if os.time() - pending.started > 25 then
        pending = nil
        error("OBS " .. command .. " did not complete within 25 seconds")
    end
    return true
end

local function state(status, detail, file)
    local data = obs.obs_data_create()
    obs.obs_data_set_string(data, "id", active_id)
    obs.obs_data_set_string(data, "status", status)
    obs.obs_data_set_string(data, "detail", detail or "")
    obs.obs_data_set_string(data, "file", file or "")
    obs.obs_data_set_bool(data, "recording", obs.obs_frontend_recording_active())
    for _, name in ipairs({"Cube", "Wwise"}) do
        local source = obs.obs_get_source_by_name("QA " .. name)
        if source ~= nil then
            obs.obs_data_set_int(data, name .. "Width", obs.obs_source_get_width(source))
            obs.obs_data_set_int(data, name .. "Height", obs.obs_source_get_height(source))
            obs.obs_source_release(source)
        end
        obs.obs_data_set_string(data, name .. "Window", targets[name] or "")
    end
    obs.obs_data_save_json(data, state_file)
    obs.obs_data_release(data)
end

local function window_targets()
    local properties = obs.obs_get_source_properties("window_capture")
    local windows = obs.obs_properties_get(properties, "window")
    for i = 0, obs.obs_property_list_item_count(windows) - 1 do
        local value = obs.obs_property_list_item_string(windows, i)
        local lower = string.lower(value)
        for _, name in ipairs({"Cube", "Wwise"}) do
            if string.match(lower, ":" .. string.lower(name) .. "%.exe$") then
                if targets[name] ~= nil and targets[name] ~= value then
                    obs.obs_properties_destroy(properties)
                    error("Multiple windows for " .. name)
                end
                targets[name] = value
            end
        end
    end
    obs.obs_properties_destroy(properties)
end

local function add_source(scene, source_id, name, settings)
    local source = obs.obs_get_source_by_name(name)
    if source == nil then
        source = obs.obs_source_create(source_id, name, settings, nil)
    else
        obs.obs_source_update(source, settings)
    end
    if source == nil then error("Source unavailable: " .. source_id) end
    local item = obs.obs_scene_find_source(scene, name)
    if item == nil then item = obs.obs_scene_add(scene, source) end
    obs.obs_source_release(source)
    return item
end

local function initialize()
    window_targets()
    if targets.Cube == nil or targets.Wwise == nil then
        state("waiting", "Cube and Wwise windows must both be running")
        return
    end
    for _, name in ipairs({"Cube", "Wwise"}) do
        local scene_source = obs.obs_get_source_by_name("Audio QA - " .. name)
        if scene_source == nil then return end
        local scene = obs.obs_scene_from_source(scene_source)
        local settings = obs.obs_data_create()
        obs.obs_data_set_string(settings, "window", targets[name])
        obs.obs_data_set_int(settings, "method", 2)
        obs.obs_data_set_int(settings, "priority", 2)
        obs.obs_data_set_bool(settings, "cursor", false)
        obs.obs_data_set_bool(settings, "client_area", false)
        obs.obs_data_set_bool(settings, "capture_audio", false)
        local item = add_source(scene, "window_capture", "QA " .. name, settings)
        obs.obs_data_release(settings)
        local bounds = obs.vec2()
        bounds.x = 1920
        bounds.y = 1080
        obs.obs_sceneitem_set_bounds_type(item, obs.OBS_BOUNDS_SCALE_INNER)
        obs.obs_sceneitem_set_bounds(item, bounds)

        local audio = obs.obs_data_create()
        obs.obs_data_set_string(audio, "window", targets.Cube)
        obs.obs_data_set_int(audio, "priority", 2)
        add_source(scene, "wasapi_process_output_capture", "QA Cube Audio", audio)
        obs.obs_data_release(audio)
        obs.obs_source_release(scene_source)
    end
    initialized = true
    state("ready", "Only Cube/Wwise windows and Cube process audio are captured")
end

local function handle_command(data)
    local command = obs.obs_data_get_string(data, "command")
    local name = obs.obs_data_get_string(data, "source")
    if command == "status" then
        state("ready")
    elseif command == "select" then
        if name ~= "Cube" and name ~= "Wwise" then error("Unknown scene") end
        local source = obs.obs_get_source_by_name("Audio QA - " .. name)
        obs.obs_frontend_set_current_scene(source)
        obs.obs_source_release(source)
        state("selected", name)
    elseif command == "screenshot" then
        if name ~= "Cube" and name ~= "Wwise" then error("Unknown source") end
        local source = obs.obs_get_source_by_name("QA " .. name)
        if obs.obs_source_get_width(source) == 0 then
            obs.obs_source_release(source)
            error("Capture source has no pixels yet")
        end
        await_result("screenshot", obs.obs_frontend_get_last_screenshot())
        obs.obs_frontend_take_source_screenshot(source)
        obs.obs_source_release(source)
    elseif command == "start" then
        if obs.obs_frontend_recording_active() then error("Already recording") end
        await_result("start")
        obs.obs_frontend_recording_start()
    elseif command == "stop" then
        if not obs.obs_frontend_recording_active() then error("Not recording") end
        await_result("stop")
        obs.obs_frontend_recording_stop()
    else
        error("Unsupported command: " .. command)
    end
end

local function tick()
    local ok, err = pcall(function()
        if not initialized then initialize(); return end
        local result, file = poll_result()
        if type(result) == "string" then state(result, "", file); return end
        if result then return end
        local data = obs.obs_data_create_from_json_file(command_file)
        if data == nil then return end
        local id = obs.obs_data_get_string(data, "id")
        if id ~= "" and id ~= last_id then
            last_id = id
            active_id = id
            handle_command(data)
        end
        obs.obs_data_release(data)
    end)
    if not ok then state("error", tostring(err)) end
end

function script_description()
    return "Audio QA: isolated window capture and Cube process audio. Local JSON commands only."
end

function script_update(settings)
    command_file = obs.obs_data_get_string(settings, "command_file")
    state_file = obs.obs_data_get_string(settings, "state_file")
end

function script_load(settings)
    script_update(settings)
    -- A previous recording command must never be replayed on restart.
    local data = obs.obs_data_create_from_json_file(command_file)
    if data ~= nil then
        last_id = obs.obs_data_get_string(data, "id")
        obs.obs_data_release(data)
    end
    obs.timer_add(tick, 250)
end

function script_unload()
    obs.timer_remove(tick)
end
