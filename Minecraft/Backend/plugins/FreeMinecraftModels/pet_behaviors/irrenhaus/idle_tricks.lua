-- Irrenhaus module (not shipped with FreeMinecraftModels; see Minecraft/docs/pets.md). Standing about, a pet now and
-- then plays one of its tricks, picked at random from the pet file's list: a dance, a salute, a claw snap. It holds
-- still while it plays, so a walk never cuts a trick short, but its owner walking off (follow_owner) or a stay order
-- always does. FMM's own idle.long keeps playing in the gaps.

-- While it rests, how often it checks whether it stands still, in ticks.
local CHECK_TICKS = 20
-- Moving less than this many blocks between two checks (probe_interval apart) counts as standing still. Measured
-- from its position, as wander does: c.pet.speed is not how fast it moves right now.
local STILL = 0.1

-- 'name:seconds' -> name, ticks; a bare 'name' plays for default_seconds.
local function parse(entry, default_seconds)
    local name, seconds = string.match(tostring(entry), '^%s*([^:]+):%s*([%d.]+)%s*$')
    if name == nil then
        name, seconds = tostring(entry), default_seconds
    end
    return name, math.max(1, math.floor(tonumber(seconds) * 20 + 0.5))
end

local function rest(c)
    local s = c.settings
    return math.floor((s.rest_seconds + math.random() * math.max(0, s.rest_random_seconds)) * 20)
end

-- True when it stood still since the last call; remembers where it is now.
local function still(c)
    local here, last = c.pet.position, c.memory:get('idle_tricks_last')
    c.memory:set('idle_tricks_last', here)
    if last == nil then return false end
    local dx, dy, dz = here.x - last.x, here.y - last.y, here.z - last.z
    return dx * dx + dy * dy + dz * dz < STILL * STILL
end

return ai.module {
    id = 'fmm:pet/idle_tricks', revision = 1,
    -- animations lists the model's animations to pick from, each 'name:seconds' (how long the pet holds still for
    -- it; a bare name holds still for seconds). They must play once: the pet Lua API cannot stop an animation, so a
    -- looping one would keep going (mc pet-build: import.play_once). A trick comes rest_seconds plus a random 0 to
    -- rest_random_seconds after the last one.
    settings = { animations = { 'idle.long' }, seconds = 3, rest_seconds = 15, rest_random_seconds = 30 },
    memories = { idle_tricks_next = 'integer', idle_tricks_until = 'integer', idle_tricks_last = 'position' },
    behaviors = {
        ai.behavior {
            -- Outranks wander (20), so a walk never starts mid-trick; follow_owner (10) and stay (5) outrank it.
            id = 'fmm:pet/idle_tricks', priority = 19, probe_interval = 10,
            controls = { ai.controls.move, ai.controls.look },
            can_start = function(c)
                local next = c.memory:get('idle_tricks_next')
                if next == nil then
                    c.memory:set('idle_tricks_next', c.tick + rest(c))
                    return false
                end
                -- Track the position every probe, so the check below compares with 10 ticks ago.
                local is_still = still(c)
                if c.tick < next then return false end
                if c.pet.staying or not c.pet.settled or not is_still then
                    c.memory:set('idle_tricks_next', c.tick + CHECK_TICKS)
                    return false
                end
                local list = c.settings.animations
                if type(list) ~= 'table' or #list == 0 then return false end
                local name, ticks = parse(list[math.random(#list)], c.settings.seconds)
                -- A model without the animation skips this turn.
                if not c.pet:play_animation(name) then
                    c.memory:set('idle_tricks_next', c.tick + rest(c))
                    return false
                end
                c.memory:set('idle_tricks_until', c.tick + ticks)
                return true
            end,
            can_continue = function(c)
                return c.tick < (c.memory:get('idle_tricks_until') or 0) and not c.pet.staying
            end,
            start = function(c) c.pet:stop() end,
            -- Nothing to do while the trick plays: it only holds the move and look controls.
            tick = function(c) end,
            stop = function(c)
                c.memory:forget('idle_tricks_until')
                c.memory:forget('idle_tricks_last')
                c.memory:set('idle_tricks_next', c.tick + rest(c))
            end
        }
    }
}
