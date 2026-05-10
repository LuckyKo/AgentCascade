# Crawlin - Wild Stories

A full dungon crawling game that uses AI for initial generation for most assets (background, character sprites, story, conversations). Inspired by old MUDs, AI dungeon and RPG card battlers, it has to provide an engaging experience for anyone at any skill level.
The game has to mainly handle the assets generation and game logic, connect with various APIs for image/sound/text generation.

It will be a big game, it needs to be properly structured with modularity in mind (no large files that contain "everything").

## Features:
 - Procedurally generated maps/enemies/bosses
 - Overworld map with multiple dungeons to conquer/defend
 - Can play both as a crew or as a dungeon keeper (defending your turf from adventurers)
 - Easily moddable but performant (no slow scripting that can bog us down)
 - Classic RPG interface that supports talk/actions/magic/items/inventory, no fixed VN style window though, visual elements can be placed anywhere on screen and adjustable by player.
 - Player selects or prompts actions, an AI will intepret them and send them as specific tools - we'll need to make an agentic framework to support it, with tool calls and all the needed scaffolding for this.
 - We'll use Open AI chat completion interface with vision support.
 - NPC characters need to be immersively played by the LLM, we'll provide it with a in person chat stack that doesnt ask it to play as a character, it tells it it IS that character. System message will look like "I am a warrior that has lived a life in... bla bla", the long term memory will be a summary of the previous encounters formatted in the inner voice of the character, the chat log of the encounter will be similarly marked by the character name on all assistant messsages, oother parties named in user messages. We will even provide a "sight" message, an image generated for what the NPC should see from it's POV (the screen if its a companion, the hero party of it's an enemy NPC)
 - Generated character/assets will be saved and reused, we can send generation calls for future encounters during active engagements.