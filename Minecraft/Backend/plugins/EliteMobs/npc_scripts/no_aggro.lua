-- Vanilla mobs never target invulnerable entities; EliteMobs only cancels NPC damage, so zombies still chase them.
-- Runs on every (re)spawn of the NPC entity. Add "no_aggro" to an NPC's scripts list to use it.
-- The npc table has no entity methods (and npc.uuid is not the entity's UUID), so find the villager at its location.
return {
 api_version=1,
 on_spawn=function(c)
  local at=c.npc:get_location()
  for _,e in ipairs(c.world:get_nearby_entities(at.x,at.y,at.z,0.5)) do
   if e.entity_type=='villager' then e:set_invulnerable(true) end
  end
 end
}
